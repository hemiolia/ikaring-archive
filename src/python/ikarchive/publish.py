"""同期の終了ごとに、保存済みデータから閲覧用の生成物を更新する。"""
from .gui import write_gui
from .xlsx_export import export_xlsx
from .store import now

def publish_outputs(store):
    root=store.output_root/'exports'
    try:
        workbook=export_xlsx(store,root/'分析.xlsx')
        page=write_gui(store,root/'gui'/'index.html')
    except Exception as exc:
        # An export failure never erases the collected records or masquerades as success.
        with store.db:
            store._control('export_error',type(exc).__name__)
            store.issue('EXPORT_FAILED',{'error_type':type(exc).__name__})
        return {'error':type(exc).__name__}
    with store.db:
        store._control('exports_updated_at',now())
        store.db.execute("DELETE FROM control WHERE key='export_error'")
    return {'xlsx':workbook,'gui':page}
