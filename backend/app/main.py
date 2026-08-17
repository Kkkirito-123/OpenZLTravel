"""OpenZLTravel 的 FastAPI 应用入口。

本文件只负责依赖装配、HTTP 路由、请求 ID 和异常映射。会话生命周期位于 runtime，
旅行规则位于 travel，外部协议位于 providers，路由层不编写业务判断。
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from re_zlagent.harness.conversation import (  # type: ignore[import-untyped]
    ConversationManager,
    ConversationPolicy,
    ModelConversationSummarizer,
    PooledPostgresConversationStore,
)
from re_zlagent.harness.model import (  # type: ignore[import-untyped]
    ModelCallBudget,
    OpenAICompatibleModelClient,
)

from app.assistant import TravelAssistantService
from app.catalog import PostgresCatalogRepository
from app.config import Settings
from app.coordination import RedisCoordination
from app.dialogue import PromptCacheTransport, TravelCommandGenerator
from app.errors import AppError
from app.identity import COOKIE_NAME, AnonymousIdentityService
from app.models import (
    AssistantMessageRequest,
    AssistantSessionView,
    AssistantSkillView,
    AssistantTurnResponse,
    DayEditRequest,
    HotelDetail,
    Itinerary,
    MemorySlotName,
    PlanningRequest,
    PlanningSelection,
    PlanningSession,
    RailOption,
    TransferSearchRequest,
    TravelMemory,
    TravelRequest,
    TripAlternatives,
    TripSummary,
    VisitorClaimRequest,
)
from app.providers import (
    AmapClient,
    AmapScheduler,
    CopyEnhancer,
    DeterministicPlanner,
    HotelProvider,
    HybridMapProvider,
    LlmPlanner,
    McpHttpClient,
    OpenMeteoClient,
    ProviderExecutor,
    RailProvider,
    RollingGoHotelClient,
)
from app.runtime import PlanningRuntime
from app.skills import list_skill_views
from app.storage import PostgresTravelRepository, create_conversation_pool
from app.travel import TravelService, itinerary_to_markdown
from app.workflow import WorkbenchWorkflow

LOGGER = logging.getLogger("openzltravel.api")


class ApplicationContainer:
    """进程级依赖容器，确保 LangGraph 和网络客户端只构造一次。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.coordination = RedisCoordination(
            self.settings.redis_url,
            {
                "amap": self.settings.amap_provider_concurrency,
                "rail": self.settings.rail_provider_concurrency,
                "hotel": self.settings.hotel_provider_concurrency,
                "llm": self.settings.llm_provider_concurrency,
            },
            timeout_seconds=self.settings.redis_timeout_seconds,
            session_lock_ttl_seconds=self.settings.session_lock_ttl_seconds,
            task_lease_ttl_seconds=self.settings.task_lease_ttl_seconds,
            task_lease_renew_seconds=self.settings.task_lease_renew_seconds,
            api_rate_limit_per_minute=self.settings.api_rate_limit_per_minute,
        )
        self.repository = PostgresTravelRepository(
            self.settings.database_url,
            min_size=self.settings.database_pool_min_size,
            max_size=self.settings.database_pool_max_size,
            timeout_seconds=self.settings.database_pool_timeout_seconds,
        )
        self.identity = AnonymousIdentityService(
            self.repository,
            secure_cookie=self.settings.visitor_cookie_secure,
            coordination=self.coordination,
        )
        self.catalog = self._catalog_repository()
        self.amap_client = AmapClient(self.settings, self.coordination)
        self.weather_client = OpenMeteoClient(self.settings, self.coordination)
        scheduler = AmapScheduler(
            concurrency=self.settings.amap_scheduler_concurrency,
            min_interval_seconds=self.settings.amap_min_interval_seconds,
            coordination=self.coordination,
        )
        map_provider = HybridMapProvider(
            catalog=self.catalog,
            upstream=self.amap_client,
            allow_amap_fallback=self.settings.allow_amap_fallback,
            weather_provider=self.weather_client,
            scheduler=scheduler,
        )
        self.travel = TravelService(
            map_provider,
            LlmPlanner(self.settings, self.coordination),
            self.repository,
        )
        rail_executor = self._executor("rail")
        hotel_executor = self._executor("hotel")
        rail_client = McpHttpClient(
            self.settings.rail_mcp_url, self.settings.rail_mcp_timeout_seconds
        )
        self.rail = RailProvider(
            rail_client,
            rail_executor,
        )
        use_rollinggo = (
            Path(self.settings.rollinggo_hotel_token_path).expanduser().is_file()
            or not self.settings.dida_api_key
        )
        hotel_source: Literal["rollinggo", "dida"]
        if use_rollinggo:
            hotel_client: McpHttpClient | RollingGoHotelClient = RollingGoHotelClient(
                self.settings.rollinggo_hotel_base_url,
                self.settings.rollinggo_hotel_token_path,
                self.settings.rollinggo_hotel_timeout_seconds,
            )
            hotel_source = "rollinggo"
        else:
            hotel_client = McpHttpClient(
                self.settings.dida_mcp_url,
                self.settings.dida_mcp_timeout_seconds,
                self.settings.dida_api_key,
            )
            hotel_source = "dida"
        self.hotel_client = hotel_client
        self.hotels = HotelProvider(hotel_client, hotel_executor, hotel_source)
        self.provider_clients: list[McpHttpClient | RollingGoHotelClient] = [
            rail_client,
            hotel_client,
        ]
        workflow = WorkbenchWorkflow(
            self.travel,
            self.rail,
            self.hotels,
            DeterministicPlanner(),
            CopyEnhancer(self.settings, self.coordination),
        )
        self.runtime = PlanningRuntime(
            self.repository,
            self.travel,
            workflow,
            self.rail,
            self.hotels,
            self.coordination,
            self.settings.recovery_scan_seconds,
        )
        self.conversation_pool = create_conversation_pool(
            self.settings.database_url,
            min_size=self.settings.conversation_pool_min_size,
            max_size=self.settings.conversation_pool_max_size,
            timeout_seconds=self.settings.database_pool_timeout_seconds,
        )
        self.conversation_store = PooledPostgresConversationStore(self.conversation_pool)
        intent_model = self._intent_model()
        summarizer = (
            ModelConversationSummarizer(
                intent_model,
                max_summary_tokens=self.settings.conversation_summary_token_limit,
                token_budget=ModelCallBudget(
                    max_input_tokens=6_000,
                    max_output_tokens=self.settings.conversation_summary_token_limit,
                ),
            )
            if intent_model
            else None
        )
        conversations = ConversationManager(
            self.conversation_store,
            summarizer=summarizer,
            policy=ConversationPolicy(
                max_recent_tokens=self.settings.conversation_recent_token_limit,
                max_summary_tokens=self.settings.conversation_summary_token_limit,
            ),
        )
        generator = (
            TravelCommandGenerator(
                intent_model,
                self.settings.intent_llm_timeout_seconds,
                cache=self.coordination,
                cache_ttl_seconds=self.settings.intent_result_cache_ttl_seconds,
                cache_namespace=(f"{self.settings.llm_base_url}|{self.settings.llm_model}"),
                max_context_chars=self.settings.intent_context_max_chars,
                coordination=self.coordination,
            )
            if intent_model
            else None
        )
        self.assistant = TravelAssistantService(
            self.repository,
            conversations,
            self.catalog,
            self.runtime,
            generator,
            self.coordination,
        )
        self._closed = False

    def _executor(self, provider: str) -> ProviderExecutor:
        return ProviderExecutor(
            provider,
            self.coordination,
            self.settings.provider_concurrency,
            self.settings.provider_failure_threshold,
            self.settings.provider_cooldown_seconds,
            self.coordination,
        )

    def _catalog_repository(self) -> PostgresCatalogRepository:
        """建立公共地点库的独立有界连接池。"""

        return PostgresCatalogRepository(
            self.settings.catalog_database_url,
            min_size=self.settings.catalog_pool_min_size,
            max_size=self.settings.catalog_pool_max_size,
            timeout_seconds=self.settings.catalog_pool_timeout_seconds,
        )

    def _intent_model(self) -> OpenAICompatibleModelClient | None:
        """仅在模型配置完整时构造意图客户端。"""

        if not self.settings.llm_api_key or not self.settings.llm_model:
            return None
        transport = (
            PromptCacheTransport(self.settings.intent_prompt_cache_key)
            if self.settings.intent_prompt_cache_key
            else None
        )
        return OpenAICompatibleModelClient(
            base_url=self.settings.llm_base_url or "https://api.openai.com/v1",
            model=self.settings.llm_model,
            api_key=self.settings.llm_api_key,
            timeout_seconds=self.settings.intent_llm_timeout_seconds,
            temperature=0,
            transport=transport,
        )

    def readiness(self) -> dict[str, object]:
        """返回不触发外部请求的本地就绪状态。"""

        database = self.repository.readiness()
        catalog = self.catalog.readiness()
        redis = self.coordination.readiness()
        ready = database == "ready" and catalog["status"] == "ready" and redis == "ready"
        return {
            "status": "ready" if ready else "not_ready",
            "database": database,
            "catalog": catalog["status"],
            "redis": redis,
            "catalog_pool": catalog["pool"],
            "rail_mcp": "configured" if self.settings.rail_mcp_url else "missing",
            "hotel_provider": self._hotel_readiness(),
            "intent_model": (
                "configured" if self.settings.llm_api_key and self.settings.llm_model else "missing"
            ),
            "prompt_cache": ("configured" if self.settings.intent_prompt_cache_key else "disabled"),
        }

    def _hotel_readiness(self) -> str:
        if isinstance(self.hotel_client, RollingGoHotelClient):
            return "rollinggo_oauth" if self.hotel_client.authenticated else "login_required"
        return "dida_token"

    async def close(self) -> None:
        """按依赖逆序停止任务并关闭全部网络与会话资源。"""

        if self._closed:
            return
        self._closed = True
        await self.runtime.close()
        results = await asyncio.gather(
            *(client.aclose() for client in self.provider_clients),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                LOGGER.warning("provider_client_close_failed", exc_info=result)
        for client in (self.amap_client, self.weather_client):
            try:
                client.close()
            except Exception:
                LOGGER.exception("map_client_close_failed")
        self.conversation_store.close()
        self.conversation_pool.close()
        self.repository.close()
        self.catalog.close()
        await self.coordination.aclose()
        self.coordination.close()


@lru_cache
def get_container() -> ApplicationContainer:
    """返回进程级应用容器。"""

    return ApplicationContainer()


def get_travel_service() -> TravelService:
    """保留旧快速入口的可覆盖依赖。"""

    return get_container().travel


def get_planning_runtime() -> PlanningRuntime:
    """返回持久规划会话运行时。"""

    return get_container().runtime


def get_assistant_service() -> TravelAssistantService:
    """返回多轮旅行助手应用服务。"""

    return get_container().assistant


def get_identity_service() -> AnonymousIdentityService:
    """返回匿名访客身份服务。"""

    return get_container().identity


def get_visitor_id(
    request: Request,
    response: Response,
    identity: AnonymousIdentityService = Depends(get_identity_service),
) -> UUID:
    """为受保护接口解析或创建浏览器级匿名身份。"""

    return identity.resolve(request, response)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动时恢复未完成会话，不主动探测外部网络。"""

    container = get_container()
    await container.runtime.recover()
    try:
        yield
    finally:
        await container.close()
        # 测试或热重载后的下一次 lifespan 必须得到新的、未关闭的容器。
        get_container.cache_clear()


app = FastAPI(title="OpenZLTravel", version="0.6.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    # Vite 在默认端口被占用时会自动选择新端口，因此仅限定本机主机名，
    # 不把开发前端绑定到固定的 5173 端口。
    allow_origin_regex=r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """为每个请求附加安全请求 ID 和耗时日志。"""

    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    started = time.perf_counter()
    if request.url.path.startswith("/api/"):
        identity = request.cookies.get(COOKIE_NAME) or (
            request.client.host if request.client else "unknown"
        )
        if not await get_container().coordination.allow_request(identity):
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "请求过于频繁，请稍后重试",
                    }
                },
                headers={"X-Request-ID": request_id},
            )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    LOGGER.info(
        "request_completed request_id=%s method=%s path=%s status=%s duration_ms=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        round((time.perf_counter() - started) * 1000),
    )
    return response


@app.exception_handler(AppError)
async def app_error_handler(_: Request, error: AppError) -> JSONResponse:
    """把稳定业务错误转换为前端统一识别的响应结构。"""

    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, error: RequestValidationError) -> JSONResponse:
    """把输入错误转换为可直接展示的中文提示。"""

    location = "、".join(str(item) for item in error.errors()[0].get("loc", []))
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
    """返回轻量存活状态。"""

    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, object]:
    """返回数据库、目录和 MCP 配置状态。"""

    return get_container().readiness()


@app.post(
    "/api/assistant-sessions",
    response_model=AssistantSessionView,
    status_code=status.HTTP_201_CREATED,
)
def create_assistant_session(
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelAssistantService = Depends(get_assistant_service),
) -> AssistantSessionView:
    """创建一个可恢复的多轮旅行助手会话。"""

    return service.create(visitor_id)


@app.get("/api/assistant-skills", response_model=list[AssistantSkillView])
def list_assistant_skills() -> list[AssistantSkillView]:
    """返回助手当前可执行的静态 Skill 契约。"""

    return list_skill_views()


@app.post("/api/visitor/claim", status_code=status.HTTP_204_NO_CONTENT)
def claim_legacy_data(
    claim: VisitorClaimRequest,
    visitor_id: UUID = Depends(get_visitor_id),
    identity: AnonymousIdentityService = Depends(get_identity_service),
) -> None:
    """用一次性认领码把旧 SQLite 数据转移给当前浏览器。"""

    identity.claim(visitor_id, claim.token)


@app.get("/api/assistant-memories", response_model=list[TravelMemory])
def list_assistant_memories(
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelAssistantService = Depends(get_assistant_service),
) -> list[TravelMemory]:
    """返回用户明确保存的长期旅行偏好。"""

    return service.list_memories(visitor_id)


@app.delete("/api/assistant-memories/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assistant_memory(
    key: MemorySlotName,
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelAssistantService = Depends(get_assistant_service),
) -> Response:
    """删除一项长期偏好，不改变已有会话任务事实。"""

    service.delete_memory(key, visitor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/api/assistant-sessions/{session_id}",
    response_model=AssistantSessionView,
)
async def get_assistant_session(
    session_id: UUID,
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelAssistantService = Depends(get_assistant_service),
) -> AssistantSessionView:
    """恢复助手状态和近期完整对话轮次。"""

    return await service.get(session_id, visitor_id)


@app.post(
    "/api/assistant-sessions/{session_id}/messages",
    response_model=AssistantTurnResponse,
)
async def send_assistant_message(
    session_id: UUID,
    message: AssistantMessageRequest,
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelAssistantService = Depends(get_assistant_service),
) -> AssistantTurnResponse:
    """识别一条消息并推进确定性旅行 Flow。"""

    return await service.send(session_id, message, visitor_id)


@app.post(
    "/api/planning-sessions",
    response_model=PlanningSession,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_planning_session(
    request: PlanningRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> PlanningSession:
    """创建后台规划会话并立即返回。"""

    return runtime.start(request, idempotency_key, visitor_id)


@app.get("/api/planning-sessions/{session_id}", response_model=PlanningSession)
def get_planning_session(
    session_id: UUID,
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> PlanningSession:
    """轮询规划会话及独立步骤状态。"""

    return runtime.get(session_id, visitor_id)


@app.put("/api/planning-sessions/{session_id}/selection", response_model=PlanningSession)
async def update_selection(
    session_id: UUID,
    selection: PlanningSelection,
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> PlanningSession:
    """保存用户选择的车次与酒店。"""

    return await runtime.update_selection(session_id, selection, visitor_id)


@app.post("/api/planning-sessions/{session_id}/rail/transfers", response_model=list[RailOption])
async def search_transfers(
    session_id: UUID,
    request: TransferSearchRequest,
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> list[RailOption]:
    """按方向加载一次中转方案。"""

    return await runtime.search_transfers(session_id, request.direction, visitor_id)


@app.get(
    "/api/planning-sessions/{session_id}/hotels/{hotel_id}",
    response_model=HotelDetail,
)
async def get_hotel_detail(
    session_id: UUID,
    hotel_id: str,
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> HotelDetail:
    """懒加载一个酒店的房型和退改规则。"""

    return await runtime.hotel_detail(session_id, hotel_id, visitor_id)


@app.post(
    "/api/planning-sessions/{session_id}/generate",
    response_model=PlanningSession,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_session(
    session_id: UUID,
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> PlanningSession:
    """调度确定性行程生成并立即返回。"""

    return await runtime.generate(session_id, visitor_id)


@app.post(
    "/api/planning-sessions/{session_id}/retry",
    response_model=PlanningSession,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_session(
    session_id: UUID,
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> PlanningSession:
    """重试失败的发现或生成阶段。"""

    return await runtime.retry(session_id, visitor_id)


@app.delete("/api/planning-sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_session(
    session_id: UUID,
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> Response:
    """取消后台任务并保留取消状态供页面确认。"""

    await runtime.cancel(session_id, visitor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/trips", response_model=Itinerary, status_code=status.HTTP_201_CREATED)
async def create_trip(
    request: TravelRequest,
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelService = Depends(get_travel_service),
) -> Itinerary:
    """兼容旧客户端的快速规划入口。"""

    return await service.create_async(request, visitor_id)


@app.get("/api/trips", response_model=list[TripSummary])
def list_trips(
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelService = Depends(get_travel_service),
) -> list[TripSummary]:
    """返回历史行程摘要。"""

    return service.list(visitor_id)


@app.get("/api/trips/{trip_id}", response_model=Itinerary)
def get_trip(
    trip_id: UUID,
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelService = Depends(get_travel_service),
) -> Itinerary:
    """返回一份完整行程。"""

    return service.get(trip_id, visitor_id)


@app.get("/api/trips/{trip_id}/alternatives", response_model=TripAlternatives)
def get_alternatives(
    trip_id: UUID,
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> TripAlternatives:
    """返回编辑时可替换的真实景点候选。"""

    session = runtime.get(trip_id, visitor_id)
    if session.candidates is None:
        raise AppError("alternatives_unavailable", "当前行程没有可用候选地点", 404)
    return runtime.travel_service.alternatives(trip_id, session.candidates, visitor_id)


@app.patch("/api/trips/{trip_id}/days/{day_index}", response_model=Itinerary)
async def edit_trip_day(
    trip_id: UUID,
    day_index: int,
    edit: DayEditRequest,
    visitor_id: UUID = Depends(get_visitor_id),
    runtime: PlanningRuntime = Depends(get_planning_runtime),
) -> Itinerary:
    """编辑一天并重算该日路线与预算。"""

    session = runtime.get(trip_id, visitor_id)
    if session.candidates is None:
        raise AppError("alternatives_unavailable", "当前行程没有可用候选地点", 404)
    return await runtime.travel_service.edit_day(
        trip_id,
        day_index,
        edit,
        session.candidates,
        visitor_id,
    )


@app.delete("/api/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(
    trip_id: UUID,
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelService = Depends(get_travel_service),
) -> Response:
    """删除指定历史行程。"""

    service.delete(trip_id, visitor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/trips/{trip_id}/export/markdown", response_class=PlainTextResponse)
def export_markdown(
    trip_id: UUID,
    visitor_id: UUID = Depends(get_visitor_id),
    service: TravelService = Depends(get_travel_service),
) -> PlainTextResponse:
    """导出指定行程的 Markdown 文本。"""

    content = itinerary_to_markdown(service.get(trip_id, visitor_id))
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="trip-{trip_id}.md"'},
    )
