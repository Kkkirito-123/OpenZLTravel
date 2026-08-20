import { describe, expect, it } from "vitest";

import { useTravelThread } from "../src/composables/useTravelThread";
import type {
  GraphRunRequest,
  StreamCallbacks,
  TravelGateway,
} from "../src/services/travelGateway";
import type {
  ResumePayload,
  ThreadSnapshot,
  TravelInterrupt,
  TravelState,
  TripRecord,
  TripSummary,
} from "../src/types";
import { createEmptyTravelState } from "../src/types";
import { draftTripFromState } from "../src/composables/travelThreadSupport";

class MemoryStorage {
  readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

class FakeGateway implements TravelGateway {
  identityCalls = 0;
  createCalls = 0;
  deleteCalls: string[] = [];
  requests: GraphRunRequest[] = [];
  reconnectCalls: Array<{ threadId: string; runId: string; eventId: string | null }> = [];
  activeRun: string | null = null;
  disconnectNext = false;
  snapshot: ThreadSnapshot = snapshot();
  trips: TripSummary[] = [tripSummary];

  async ensureIdentity(): Promise<void> {
    this.identityCalls += 1;
  }

  async createThread(): Promise<string> {
    this.createCalls += 1;
    return `thread-${this.createCalls}`;
  }

  async loadThread(): Promise<ThreadSnapshot> {
    return this.snapshot;
  }

  async findActiveRun(): Promise<string | null> {
    return this.activeRun;
  }

  async streamRun(
    _threadId: string,
    request: GraphRunRequest,
    callbacks: StreamCallbacks,
  ): Promise<void> {
    this.requests.push(request);
    callbacks.onCursor("run-1", "event-1");
    if (this.disconnectNext) {
      this.disconnectNext = false;
      throw new Error("连接中断");
    }
    callbacks.onUpdate("planner_agent");
    callbacks.onSnapshot(this.snapshot);
  }

  async reconnectRun(
    threadId: string,
    runId: string,
    eventId: string | null,
    callbacks: StreamCallbacks,
  ): Promise<void> {
    this.reconnectCalls.push({ threadId, runId, eventId });
    callbacks.onSnapshot(this.snapshot);
  }

  async listTrips(): Promise<TripSummary[]> {
    return [...this.trips];
  }

  async getTrip(): Promise<TripRecord> {
    return tripRecord;
  }

  async deleteTrip(tripId: string): Promise<void> {
    this.deleteCalls.push(tripId);
    this.trips = this.trips.filter((trip) => trip.trip_id !== tripId);
  }
}

describe("useTravelThread", () => {
  it("初始状态工厂不共享可变集合，草稿投影保持事实索引", () => {
    const first = createEmptyTravelState();
    const second = createEmptyTravelState();
    first.messages.push({ role: "user", content: "去杭州" });

    expect(second.messages).toEqual([]);
    expect(draftTripFromState({
      ...first,
      facts: { hotel_options: [{ hotel_id: "hotel-1", name: "湖滨酒店" }] },
      draft: {
        summary: "杭州行程",
        days: [{ day_index: 1, theme: "湖滨", activities: [], hotel_id: "hotel-1" }],
      },
    })?.place_index?.["hotel-1"]?.name).toBe("湖滨酒店");
  });

  it("创建并持久化唯一 Thread，发送标准 Graph 输入", async () => {
    const gateway = new FakeGateway();
    gateway.snapshot = snapshot({
      messages: [
        { role: "user", content: "上海出发去杭州三天" },
        { role: "assistant", content: "需求已确认，正在查询真实候选。" },
      ],
      phase: "discovering",
    });
    const storage = new MemoryStorage();
    const travel = useTravelThread({ gateway, storage, autoInitialize: false });

    await travel.initialize();
    await travel.submitMessage("上海出发去杭州三天");

    expect(gateway.identityCalls).toBe(1);
    expect(storage.getItem("openzltravel.travel_thread_id")).toBe("thread-1");
    expect(gateway.requests[0]).toEqual({
      input: { messages: [{ role: "user", content: "上海出发去杭州三天" }] },
    });
    expect(travel.state.value.phase).toBe("discovering");
    expect(travel.messages.value.at(-1)?.role).toBe("assistant");
  });

  it.each<[
    TravelInterrupt,
    ResumePayload,
  ]>([
    [
      { kind: "clarification", question: "什么时候出发？", missing_fields: ["start_date"] },
      { kind: "clarification", values: { start_date: "2026-10-01" } },
    ],
    [
      {
        kind: "destination_selection",
        candidates: [{ candidate_id: "hz", city: { name: "杭州" }, score: 0.9, reasons: [] }],
      },
      { kind: "destination_selection", candidate_id: "hz" },
    ],
    [
      {
        kind: "travel_selection",
        outbound_options: [],
        return_options: [],
        hotel_options: [],
        requires_hotel: true,
        self_arranged_allowed: true,
      },
      {
        kind: "travel_selection",
        selection: {
          self_arranged_outbound: true,
          self_arranged_return: true,
          self_arranged_hotel: true,
        },
      },
    ],
  ])("使用与 interrupt 匹配的 Command resume：%s", async (interrupt, payload) => {
    const gateway = new FakeGateway();
    gateway.snapshot = snapshot({}, interrupt);
    const storage = new MemoryStorage();
    storage.setItem("openzltravel.travel_thread_id", "existing-thread");
    const travel = useTravelThread({ gateway, storage, autoInitialize: false });

    await travel.initialize();
    await travel.resume(payload);

    expect(gateway.requests).toEqual([{ resume: payload }]);
  });

  it("拒绝错误类型的 resume 且不推进状态", async () => {
    const gateway = new FakeGateway();
    gateway.snapshot = snapshot({}, {
      kind: "destination_selection",
      candidates: [],
    });
    const storage = new MemoryStorage();
    storage.setItem("openzltravel.travel_thread_id", "existing-thread");
    const travel = useTravelThread({ gateway, storage, autoInitialize: false });

    await travel.initialize();
    await travel.resume({ kind: "clarification", values: { destination: "杭州" } });

    expect(gateway.requests).toHaveLength(0);
    expect(travel.error.value).toContain("不匹配");
  });

  it("流断开后携带 Run 与 Event 游标恢复", async () => {
    const gateway = new FakeGateway();
    gateway.disconnectNext = true;
    gateway.snapshot = snapshot({ phase: "completed", trip_id: "trip-1", draft: tripRecord.draft });
    const travel = useTravelThread({
      gateway,
      storage: new MemoryStorage(),
      autoInitialize: false,
    });

    await travel.initialize();
    await travel.submitMessage("去杭州");
    expect(travel.disconnected.value).toBe(true);

    await travel.reconnect();

    expect(gateway.reconnectCalls).toEqual([
      { threadId: "thread-1", runId: "run-1", eventId: "event-1" },
    ]);
    expect(travel.disconnected.value).toBe(false);
    expect(travel.state.value.phase).toBe("completed");
  });

  it("开始新行程时替换 Thread 并清空当前状态", async () => {
    const gateway = new FakeGateway();
    const storage = new MemoryStorage();
    const travel = useTravelThread({ gateway, storage, autoInitialize: false });

    await travel.initialize();
    await travel.startNewTrip();

    expect(travel.threadId.value).toBe("thread-2");
    expect(storage.getItem("openzltravel.travel_thread_id")).toBe("thread-2");
    expect(travel.state.value.phase).toBe("collecting");
  });

  it("读取并删除 Store 行程历史", async () => {
    const gateway = new FakeGateway();
    const travel = useTravelThread({
      gateway,
      storage: new MemoryStorage(),
      autoInitialize: false,
    });
    await travel.initialize();

    await travel.openHistory();
    expect(travel.history.value).toEqual([tripSummary]);

    await travel.deleteHistoricalTrip("trip-1");
    expect(gateway.deleteCalls).toEqual(["trip-1"]);
    expect(travel.history.value).toEqual([]);
  });

  it("执行中草稿使用实时酒店事实构建展示索引", async () => {
    const gateway = new FakeGateway();
    gateway.snapshot = snapshot({
      phase: "planning",
      requirements: { destination: "杭州" },
      facts: {
        hotel_options: [{ hotel_id: "rollinggo-1", name: "湖滨实时酒店" }],
      },
      draft: {
        summary: "杭州行程",
        days: [{ day_index: 1, theme: "湖滨", activities: [], hotel_id: "rollinggo-1" }],
      },
    });
    const storage = new MemoryStorage();
    storage.setItem("openzltravel.travel_thread_id", "existing-thread");
    const travel = useTravelThread({ gateway, storage, autoInitialize: false });

    await travel.initialize();

    expect(travel.currentTrip.value?.place_index?.["rollinggo-1"]?.name)
      .toBe("湖滨实时酒店");
  });
});

function snapshot(
  update: Partial<TravelState> = {},
  interrupt: TravelInterrupt | null = null,
): ThreadSnapshot {
  return {
    status: interrupt ? "interrupted" : "idle",
    interrupt,
    state: {
      messages: [],
      phase: "collecting",
      requirements: {},
      destination_candidates: [],
      facts: {},
      selection: {},
      warnings: [],
      errors: [],
      revision_count: 0,
      ...update,
    },
  };
}

const tripSummary: TripSummary = {
  trip_id: "trip-1",
  destination: "杭州",
  start_date: "2026-10-01",
  end_date: "2026-10-03",
  summary: "西湖与人文三日游",
};

const tripRecord: TripRecord = {
  trip_id: "trip-1",
  requirements: { destination: "杭州", start_date: "2026-10-01", end_date: "2026-10-03" },
  city: { name: "杭州" },
  draft: {
    summary: "西湖与人文三日游",
    days: [{
      day_index: 1,
      theme: "西湖初见",
      activities: [{ poi_id: "poi-1", start_time: "09:00", duration_minutes: 180 }],
    }],
  },
  place_index: {
    "poi-1": {
      fact_id: "poi-1",
      name: "西湖",
      address: "杭州市西湖区",
      category: "attraction",
    },
  },
  warnings: [],
};
