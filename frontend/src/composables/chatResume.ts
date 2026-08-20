import type {
  ClarificationInterrupt,
  DestinationSelectionInterrupt,
  ResumePayload,
  TravelInterrupt,
  TravelSelectionInterrupt,
} from "../types";

/**
 * 把聊天中的“我选第一个/酒店选某某”转换成稳定的 interrupt resume。
 *
 * 这里不是让前端修改事实，而是把用户的自然语言映射到当前 interrupt 已公开的
 * 候选 ID。最终仍由后端重新校验 ID，所以即使前端解析出错，也不会绕过事实边界。
 */
export interface ChatResumeResult {
  payload?: ResumePayload;
  error?: string;
}

export function parseChatResume(
  interrupt: TravelInterrupt,
  text: string,
): ChatResumeResult {
  const normalized = text.trim();
  if (!normalized) return { error: "请输入要确认的内容。" };
  if (interrupt.kind === "clarification") {
    return parseClarification(interrupt, normalized);
  }
  if (interrupt.kind === "destination_selection") {
    return parseDestination(interrupt, normalized);
  }
  return parseTravelSelection(interrupt, normalized);
}

function parseClarification(
  interrupt: ClarificationInterrupt,
  text: string,
): ChatResumeResult {
  const values: Record<string, unknown> = {};
  const dates = [...text.matchAll(/(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})/g)]
    .map((match) => `${match[1]}-${match[2].padStart(2, "0")}-${match[3].padStart(2, "0")}`);
  if (dates[0]) values.start_date = dates[0];
  if (dates[1]) values.end_date = dates[1];

  const days = text.match(/(\d+)\s*(?:天|日)/);
  if (days) values.trip_days = Number(days[1]);
  const travelers = text.match(/(\d+)\s*(?:人|位)/);
  if (travelers) values.travelers = Number(travelers[1]);
  const budget = text.match(/预算\s*([\d.]+)\s*(?:元|块)?/);
  if (budget) values.budget = Number(budget[1]);

  const origin = text.match(/从\s*([^，。,、\s]+)\s*出发/);
  if (origin) values.origin = origin[1];
  const destination = text.match(/(?:去|到)\s*([^，。,、\s]+?)(?=玩|旅游|旅行|度假|，|,|$)/);
  if (destination) values.destination = destination[1];
  const region = text.match(/(华东|华南|华北|华中|西南|西北|东北|江浙沪|长三角)/);
  if (region) values.region = region[1];

  const preference = text.match(/喜欢\s*([^。！？!?，,]+)/);
  if (preference) {
    values.preferences = preference[1]
      .split(/[、和及\s]+/)
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 8);
  }

  const missing = interrupt.missing_fields;
  const hasRelevantValue = Object.keys(values).some((key) =>
    missing.includes(key) || ["budget", "travelers", "preferences"].includes(key),
  );
  if (!hasRelevantValue) {
    return { error: `我还无法从这句话确认${missing.join("、")}，请补充具体值。` };
  }
  return { payload: { kind: "clarification", values } };
}

function parseDestination(
  interrupt: DestinationSelectionInterrupt,
  text: string,
): ChatResumeResult {
  const index = text.match(/第\s*(\d+)\s*(?:个|项|座)?/);
  const candidate = index
    ? interrupt.candidates[Number(index[1]) - 1]
    : interrupt.candidates.find((item) => text.includes(item.city.name));
  if (!candidate) {
    return { error: "请回复候选城市名称，或回复“第 1 个”。" };
  }
  return {
    payload: { kind: "destination_selection", candidate_id: candidate.candidate_id },
  };
}

function parseTravelSelection(
  interrupt: TravelSelectionInterrupt,
  text: string,
): ChatResumeResult {
  const selection: NonNullable<Extract<ResumePayload, { kind: "travel_selection" }>['selection']> = {
    outbound: null,
    return_trip: null,
    hotel_id: null,
    self_arranged_outbound: false,
    self_arranged_return: false,
    self_arranged_hotel: false,
  };

  const selfArranged = /自行安排|自己安排|不需要(?:车票|交通|酒店|住宿)?/.test(text);
  if (!interrupt.outbound_options.length || /去程[^。！？!?，,]*(?:自行|自己|不需要)/.test(text)) {
    selection.self_arranged_outbound = selfArranged || !interrupt.outbound_options.length;
  }
  if (!interrupt.return_options.length || /返程[^。！？!?，,]*(?:自行|自己|不需要)/.test(text)) {
    selection.self_arranged_return = selfArranged || !interrupt.return_options.length;
  }
  if (!interrupt.hotel_options.length || /(?:酒店|住宿)[^。！？!?，,]*(?:自行|自己|不需要)/.test(text)) {
    selection.self_arranged_hotel = selfArranged || !interrupt.hotel_options.length;
  }

  selection.outbound = choiceFromText(text, interrupt.outbound_options, "去程");
  selection.return_trip = choiceFromText(text, interrupt.return_options, "返程");
  const hotel = optionFromText(text, interrupt.hotel_options, "酒店|住宿");
  if (hotel) selection.hotel_id = hotel.hotel_id;

  if (interrupt.outbound_options.length && !selection.outbound && !selection.self_arranged_outbound) {
    return { error: "请确认去程车次，或回复“去程自行安排”。" };
  }
  if (interrupt.return_options.length && !selection.return_trip && !selection.self_arranged_return) {
    return { error: "请确认返程车次，或回复“返程自行安排”。" };
  }
  if (interrupt.requires_hotel && interrupt.hotel_options.length
    && !selection.hotel_id && !selection.self_arranged_hotel) {
    return { error: "请确认酒店，或回复“酒店自行安排”。" };
  }
  return { payload: { kind: "travel_selection", selection } };
}

function choiceFromText(
  text: string,
  options: TravelSelectionInterrupt["outbound_options"],
  label: string,
) {
  const option = optionFromText(text, options, label);
  return option ? { option_id: option.option_id, seat_type: option.seats?.[0]?.name ?? null } : null;
}

function optionFromText<T extends { option_id?: string; hotel_id?: string; name?: string }>(
  text: string,
  options: T[],
  label: string,
): T | undefined {
  if (!options.length) return undefined;
  // ``label`` 允许使用“酒店|住宿”这样的别名，必须整体分组；否则正则中的
  // ``|`` 会把后半段变成独立分支，导致“酒店选第 1 个”无法取到序号。
  const labels = label
    .split("|")
    .map((value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|");
  const number = text.match(
    new RegExp(`(?:${labels})[^第\\d]{0,8}第?\\s*(\\d+)\\s*(?:个|项|家)?`),
  );
  if (number) return options[Number(number[1]) - 1];
  return options.find((option) => option.name && text.includes(option.name));
}
