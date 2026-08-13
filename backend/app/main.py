"""OpenZLTravel 的 FastAPI 应用入口。

本文件只负责应用装配、HTTP 路由和异常映射；旅行规则位于 travel.py，外部服务与
数据库实现分别位于 providers.py 和 storage.py，避免路由层混入业务判断。
"""

from functools import lru_cache
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import Settings
from app.errors import AppError
from app.models import Itinerary, TravelRequest, TripSummary
from app.providers import AmapClient, HybridMapProvider, LlmPlanner
from app.storage import CatalogRepository, SqliteTripRepository
from app.travel import TravelService, itinerary_to_markdown


@lru_cache
def get_travel_service() -> TravelService:
    """组装进程级旅行服务，测试可通过 FastAPI 依赖覆盖替换。"""

    settings = Settings()
    amap = AmapClient(settings)
    return TravelService(
        map_provider=HybridMapProvider(
            catalog=CatalogRepository(settings.catalog_path),
            upstream=amap,
            allow_amap_fallback=settings.allow_amap_fallback,
        ),
        planner=LlmPlanner(settings),
        repository=SqliteTripRepository(settings.database_path),
    )


app = FastAPI(title="OpenZLTravel", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, error: AppError) -> JSONResponse:
    """把稳定业务错误转换为前端统一识别的响应结构。"""

    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, error: RequestValidationError) -> JSONResponse:
    """把 Pydantic 输入错误转换成可直接展示的中文提示。"""

    first_error = error.errors()[0]
    location = "、".join(str(item) for item in first_error.get("loc", []))
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": f"请求参数“{location}”不合法",
            }
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    """返回应用存活状态。"""

    return {"status": "ok"}


@app.post("/api/trips", response_model=Itinerary, status_code=status.HTTP_201_CREATED)
def create_trip(
    request: TravelRequest,
    service: TravelService = Depends(get_travel_service),
) -> Itinerary:
    """生成并自动保存一份行程。"""

    return service.create(request)


@app.get("/api/trips", response_model=list[TripSummary])
def list_trips(service: TravelService = Depends(get_travel_service)) -> list[TripSummary]:
    """返回历史行程摘要。"""

    return service.list()


@app.get("/api/trips/{trip_id}", response_model=Itinerary)
def get_trip(
    trip_id: UUID,
    service: TravelService = Depends(get_travel_service),
) -> Itinerary:
    """返回一份完整行程。"""

    return service.get(trip_id)


@app.delete("/api/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(
    trip_id: UUID,
    service: TravelService = Depends(get_travel_service),
) -> Response:
    """删除指定历史行程。"""

    service.delete(trip_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/trips/{trip_id}/export/markdown", response_class=PlainTextResponse)
def export_markdown(
    trip_id: UUID,
    service: TravelService = Depends(get_travel_service),
) -> PlainTextResponse:
    """导出指定行程的 Markdown 文本。"""

    content = itinerary_to_markdown(service.get(trip_id))
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="trip-{trip_id}.md"'},
    )
