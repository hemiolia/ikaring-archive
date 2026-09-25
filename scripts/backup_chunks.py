#!/usr/bin/env python3
"""Split a verified ciphertext into uploadable parts and reconstruct it safely."""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path

MAX_CHUNK_SIZE=256*1024*1024
DEFAULT_CHUNK_SIZE=64*1024*1024
CHUNK_READ_SIZE=1024*1024
FORMAT='ikaring-archive-chunks-v1'
SHA256_PATTERN=re.compile(r'[0-9a-f]{64}\Z')

class ChunkError(RuntimeError):
    pass

def regular_identity(path):
    if path.is_symlink():raise ChunkError('SYMLINK_REFUSED: '+path.name)
    info=path.stat()
    if not stat.S_ISREG(info.st_mode):raise ChunkError('REGULAR_FILE_REQUIRED: '+path.name)
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def ensure_unchanged(path,identity):
    if regular_identity(path)!=identity:raise ChunkError('SOURCE_CHANGED: '+path.name)

def safe_basename(name):
    if (not isinstance(name,str) or not name or name in {'.','..'} or
            '/' in name or '\\' in name or '\x00' in name or Path(name).name!=name):
        raise ChunkError('UNSAFE_BASENAME')
    return name

def chunk_manifest_name(cipher_name):
    return safe_basename(cipher_name)+'.chunks.json'

def part_name(cipher_name,index):
    return safe_basename(cipher_name)+f'.part-{index:06d}'

def valid_sha256(value):
    if not isinstance(value,str) or not SHA256_PATTERN.fullmatch(value):
        raise ChunkError('INVALID_SHA256')
    return value

def positive_int(value):
    if type(value) is not int or value<=0:raise ChunkError('INVALID_SIZE_OR_INDEX')
    return value

def load_encrypted_manifest(content,cipher_name,cipher_bytes,cipher_sha):
    try:data=json.loads(content)
    except (ValueError,UnicodeError) as exc:raise ChunkError('ENCRYPTED_MANIFEST_INVALID_JSON') from exc
    if not isinstance(data,dict) or set(data)!={'timestamp','raw_snapshot','encrypted_snapshot','verification'}:
        raise ChunkError('ENCRYPTED_MANIFEST_INVALID_FIELDS')
    raw=data['raw_snapshot'];enc=data['encrypted_snapshot'];verification=data['verification']
    if (not isinstance(raw,dict) or set(raw)!={'basename','bytes','sha256','quick_check'} or
            not isinstance(enc,dict) or not {'basename','bytes','sha256','compression','cipher'}<=set(enc) or
            set(enc)-{'basename','bytes','sha256','compression','cipher','md5'} or
            not isinstance(verification,dict) or set(verification)!={'sha256_match','quick_check'}):
        raise ChunkError('ENCRYPTED_MANIFEST_INVALID_FIELDS')
    safe_basename(raw['basename']);positive_int(raw['bytes']);valid_sha256(raw['sha256'])
    if (raw['quick_check']!='ok' or verification['sha256_match'] is not True or
            verification['quick_check']!='ok' or enc['compression']!='zstd' or enc['cipher']!='AES256'):
        raise ChunkError('ENCRYPTED_MANIFEST_NOT_VERIFIED')
    if enc['basename']!=cipher_name or type(enc['bytes']) is not int or enc['bytes']!=cipher_bytes or enc['sha256']!=cipher_sha:
        raise ChunkError('ENCRYPTED_MANIFEST_CIPHERTEXT_MISMATCH')
    if 'md5' in enc and (not isinstance(enc['md5'],str) or not re.fullmatch(r'[0-9a-f]{32}\Z',enc['md5'])):
        raise ChunkError('ENCRYPTED_MANIFEST_INVALID_MD5')
    if not isinstance(data['timestamp'],str):raise ChunkError('ENCRYPTED_MANIFEST_INVALID_TIMESTAMP')
    return data

def hash_file(path):
    digest=hashlib.sha256();md5=hashlib.md5();total=0
    with path.open('rb') as stream:
        while chunk:=stream.read(CHUNK_READ_SIZE):
            digest.update(chunk);md5.update(chunk);total+=len(chunk)
    return total,digest.hexdigest(),md5.hexdigest()

def fsync_directory(directory):
    fd=os.open(directory,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def write_json_last(directory,manifest,filename):
    fd,name=tempfile.mkstemp(prefix='.tmp_manifest_',dir=directory)
    temp=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(manifest,stream,ensure_ascii=False,indent=2)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.link(temp,directory/filename)
        fsync_directory(directory)
    finally:temp.unlink(missing_ok=True)

def remove_own_bundle(directory):
    for child in directory.iterdir():
        if child.is_file() or child.is_symlink():child.unlink()
    directory.rmdir()

def split(ciphertext,encrypted_manifest,output_dir,chunk_size=DEFAULT_CHUNK_SIZE):
    ciphertext=Path(ciphertext).absolute()
    encrypted_manifest=Path(encrypted_manifest).absolute()
    output_dir=Path(output_dir).absolute()
    if type(chunk_size) is not int or not 1<=chunk_size<=MAX_CHUNK_SIZE:
        raise ChunkError('CHUNK_SIZE_OUT_OF_RANGE')
    cipher_identity=regular_identity(ciphertext)
    manifest_identity=regular_identity(encrypted_manifest)
    if ciphertext==encrypted_manifest:raise ChunkError('SOURCE_AND_MANIFEST_ARE_SAME_FILE')
    safe_basename(ciphertext.name)
    manifest_name=safe_basename(encrypted_manifest.name)
    chunk_name=chunk_manifest_name(ciphertext.name)
    if manifest_name==chunk_name:raise ChunkError('ENCRYPTED_MANIFEST_NAME_CONFLICT')
    if manifest_identity[2]>1024*1024:raise ChunkError('ENCRYPTED_MANIFEST_TOO_LARGE')
    manifest_bytes=encrypted_manifest.read_bytes()
    ensure_unchanged(encrypted_manifest,manifest_identity)
    if output_dir.is_symlink():raise ChunkError('OUTPUT_SYMLINK_REFUSED')
    output_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    if len(ciphertext.name)>170:raise ChunkError('CIPHERTEXT_BASENAME_TOO_LONG')
    bundle=output_dir/(ciphertext.name+'.chunks.'+uuid.uuid4().hex)
    bundle.mkdir(mode=0o700)
    complete=False
    try:
        parts=[];whole_sha=hashlib.sha256();whole_md5=hashlib.md5();total=0
        flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
        fd=os.open(ciphertext,flags)
        with os.fdopen(fd,'rb') as source:
            info=os.fstat(source.fileno())
            if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=cipher_identity:
                raise ChunkError('SOURCE_CHANGED')
            index=0
            while True:
                block=source.read(chunk_size)
                if not block:break
                name=part_name(ciphertext.name,index)
                part_path=bundle/name
                part_sha=hashlib.sha256()
                with part_path.open('xb') as part:
                    if part.write(block)!=len(block):raise ChunkError('PART_SHORT_WRITE')
                    part_sha.update(block)
                    part.flush();os.fsync(part.fileno())
                whole_sha.update(block);whole_md5.update(block);total+=len(block)
                parts.append({'index':index,'basename':name,'bytes':len(block),'sha256':part_sha.hexdigest()})
                index+=1
        ensure_unchanged(ciphertext,cipher_identity)
        if total!=cipher_identity[2] or not parts:raise ChunkError('SOURCE_SIZE_MISMATCH')
        encrypted_data=load_encrypted_manifest(manifest_bytes,ciphertext.name,total,whole_sha.hexdigest())
        if 'md5' in encrypted_data['encrypted_snapshot'] and encrypted_data['encrypted_snapshot']['md5']!=whole_md5.hexdigest():
            raise ChunkError('ENCRYPTED_MANIFEST_MD5_MISMATCH')
        copied_manifest=bundle/manifest_name
        with copied_manifest.open('xb') as target:
            target.write(manifest_bytes);target.flush();os.fsync(target.fileno())
        ensure_unchanged(ciphertext,cipher_identity)
        ensure_unchanged(encrypted_manifest,manifest_identity)
        manifest={
            'format':FORMAT,
            'ciphertext':{'basename':ciphertext.name,'bytes':total,'sha256':whole_sha.hexdigest()},
            'encrypted_manifest':{'basename':manifest_name,'bytes':len(manifest_bytes),'sha256':hashlib.sha256(manifest_bytes).hexdigest()},
            'chunk_size':chunk_size,
            'parts':parts,
        }
        write_json_last(bundle,manifest,chunk_name)
        complete=True
        return {'status':'ok','bundle_dir':str(bundle),'chunk_manifest':str(bundle/chunk_name),'parts':len(parts)}
    finally:
        if not complete:remove_own_bundle(bundle)

def validate_chunk_manifest(data):
    if not isinstance(data,dict) or set(data)!={'format','ciphertext','encrypted_manifest','chunk_size','parts'} or data['format']!=FORMAT:
        raise ChunkError('CHUNK_MANIFEST_INVALID_FIELDS')
    cipher=data['ciphertext'];reference=data['encrypted_manifest'];parts=data['parts']
    if not isinstance(cipher,dict) or set(cipher)!={'basename','bytes','sha256'}:
        raise ChunkError('CHUNK_MANIFEST_INVALID_CIPHERTEXT')
    safe_basename(cipher['basename']);positive_int(cipher['bytes']);valid_sha256(cipher['sha256'])
    if not isinstance(reference,dict) or set(reference)!={'basename','bytes','sha256'}:
        raise ChunkError('CHUNK_MANIFEST_INVALID_REFERENCE')
    safe_basename(reference['basename']);positive_int(reference['bytes']);valid_sha256(reference['sha256'])
    if reference['basename']==chunk_manifest_name(cipher['basename']):raise ChunkError('CHUNK_MANIFEST_REFERENCE_CONFLICT')
    chunk_size=positive_int(data['chunk_size'])
    if chunk_size>MAX_CHUNK_SIZE:raise ChunkError('CHUNK_SIZE_OUT_OF_RANGE')
    if not isinstance(parts,list) or not parts:raise ChunkError('CHUNK_MANIFEST_NO_PARTS')
    total=0
    for index,part in enumerate(parts):
        if not isinstance(part,dict) or set(part)!={'index','basename','bytes','sha256'}:
            raise ChunkError('CHUNK_MANIFEST_INVALID_PART')
        if type(part['index']) is not int or part['index']!=index or part['basename']!=part_name(cipher['basename'],index):
            raise ChunkError('CHUNK_MANIFEST_PART_ORDER')
        size=positive_int(part['bytes'])
        if size>chunk_size or (index<len(parts)-1 and size!=chunk_size):
            raise ChunkError('CHUNK_MANIFEST_PART_SIZE')
        valid_sha256(part['sha256']);total+=size
    if total!=cipher['bytes']:raise ChunkError('CHUNK_MANIFEST_TOTAL_SIZE')
    return cipher,reference,parts

def join(chunk_manifest,output):
    chunk_manifest=Path(chunk_manifest).absolute()
    output=Path(output).absolute()
    if not chunk_manifest.name.endswith('.chunks.json'):raise ChunkError('CHUNK_MANIFEST_NAME_REQUIRED')
    bundle=chunk_manifest.parent
    if bundle.is_symlink():raise ChunkError('BUNDLE_SYMLINK_REFUSED')
    manifest_identity=regular_identity(chunk_manifest)
    if manifest_identity[2]>16*1024*1024:raise ChunkError('CHUNK_MANIFEST_TOO_LARGE')
    try:data=json.loads(chunk_manifest.read_text(encoding='utf-8'))
    except (ValueError,UnicodeError) as exc:raise ChunkError('CHUNK_MANIFEST_INVALID_JSON') from exc
    ensure_unchanged(chunk_manifest,manifest_identity)
    cipher,reference,parts=validate_chunk_manifest(data)
    if chunk_manifest.name!=chunk_manifest_name(cipher['basename']):raise ChunkError('CHUNK_MANIFEST_NAME_MISMATCH')
    encrypted_manifest=bundle/reference['basename']
    encrypted_identity=regular_identity(encrypted_manifest)
    if encrypted_identity[2]!=reference['bytes']:raise ChunkError('ENCRYPTED_MANIFEST_SIZE_MISMATCH')
    content=encrypted_manifest.read_bytes()
    ensure_unchanged(encrypted_manifest,encrypted_identity)
    if hashlib.sha256(content).hexdigest()!=reference['sha256']:
        raise ChunkError('ENCRYPTED_MANIFEST_SHA256_MISMATCH')
    load_encrypted_manifest(content,cipher['basename'],cipher['bytes'],cipher['sha256'])
    if output.exists() or output.is_symlink():raise ChunkError('DESTINATION_EXISTS')
    if not output.parent.is_dir() or output.parent.is_symlink():raise ChunkError('DESTINATION_PARENT_INVALID')
    fd,temp_name=tempfile.mkstemp(prefix='.tmp_join_',dir=output.parent)
    temp=Path(temp_name)
    try:
        whole=hashlib.sha256();total=0
        with os.fdopen(fd,'wb') as target:
            for part in parts:
                part_path=bundle/part['basename']
                identity=regular_identity(part_path)
                if identity[2]!=part['bytes']:raise ChunkError('PART_SIZE_MISMATCH: '+part['basename'])
                digest=hashlib.sha256();count=0
                flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
                input_fd=os.open(part_path,flags)
                with os.fdopen(input_fd,'rb') as source:
                    while chunk:=source.read(CHUNK_READ_SIZE):
                        digest.update(chunk);whole.update(chunk);count+=len(chunk);total+=len(chunk)
                        target.write(chunk)
                ensure_unchanged(part_path,identity)
                if count!=part['bytes'] or digest.hexdigest()!=part['sha256']:
                    raise ChunkError('PART_HASH_MISMATCH: '+part['basename'])
            if total!=cipher['bytes'] or whole.hexdigest()!=cipher['sha256']:
                raise ChunkError('CIPHERTEXT_HASH_MISMATCH')
            ensure_unchanged(chunk_manifest,manifest_identity)
            ensure_unchanged(encrypted_manifest,encrypted_identity)
            target.flush();os.fsync(target.fileno())
        os.link(temp,output)
        fsync_directory(output.parent)
        return {'status':'ok','ciphertext_path':str(output),'bytes':total,'sha256':whole.hexdigest()}
    finally:temp.unlink(missing_ok=True)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    split_parser=commands.add_parser('split')
    split_parser.add_argument('--ciphertext',required=True,type=Path)
    split_parser.add_argument('--encrypted-manifest',required=True,type=Path)
    split_parser.add_argument('--output-dir',required=True,type=Path)
    split_parser.add_argument('--chunk-size',type=int,default=DEFAULT_CHUNK_SIZE)
    join_parser=commands.add_parser('join')
    join_parser.add_argument('--chunk-manifest',required=True,type=Path)
    join_parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        result=split(args.ciphertext,args.encrypted_manifest,args.output_dir,args.chunk_size) if args.command=='split' else join(args.chunk_manifest,args.output)
    except (OSError,ValueError,ChunkError) as exc:
        print('Chunk operation failed: '+str(exc),file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':sys.exit(main())
