"""月结关账入口：生成版本、查当前/历史版本、导出 CSV 与 JSON。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response

from ...errors import DomainError
from . import export_out, service

router = APIRouter(prefix="/api/closes", tags=["月结关账"])


@router.post("")
async def create_close(
    request: Request,
    period: str = Form(""),
    sample: str = Form(""),
    file: UploadFile | None = File(None),
):
    settings = request.app.state.settings
    store = request.app.state.store
    if file is not None and file.filename:
        raw = await file.read()
        filename = file.filename
    elif sample:
        from ..m1_ingest.service import resolve_sample

        filename, raw = resolve_sample(settings, sample)
    else:
        raise DomainError("E_INPUT", "需要上传期末余额文件或指定样例", 400)
    return service.create_close(settings, store, filename, raw, period or None)


@router.get("")
def list_closes(request: Request):
    return service.history_payload(request.app.state.store)


@router.get("/latest")
def latest_close(request: Request):
    payload = service.latest_payload(request.app.state.store)
    if payload is None:
        raise DomainError("E_NO_CLOSE", "还没有关账版本", 404)
    return payload


@router.get("/{close_id}")
def get_close(request: Request, close_id: str):
    return service.detail_payload(request.app.state.store, close_id)


@router.get("/latest/close.csv")
def latest_close_csv(request: Request, version: str = ""):
    store = request.app.state.store
    payload = (service.detail_payload(store, version) if version
               else service.latest_payload(store))
    if payload is None:
        raise DomainError("E_NO_CLOSE", "还没有关账版本", 404)
    filename = f"close_{payload['version']['close_id']}.csv"
    return Response(
        content=export_out.close_csv_text(payload),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/latest/close.json")
def latest_close_json(request: Request, version: str = ""):
    store = request.app.state.store
    payload = (service.detail_payload(store, version) if version
               else service.latest_payload(store))
    if payload is None:
        raise DomainError("E_NO_CLOSE", "还没有关账版本", 404)
    filename = f"close_{payload['version']['close_id']}.json"
    return Response(
        content=export_out.close_json_text(payload),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
