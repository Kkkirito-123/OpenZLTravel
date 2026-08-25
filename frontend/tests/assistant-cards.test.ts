import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import AssistantCards from "../src/features/assistant/AssistantCards.vue";
import { emptyAssistantSnapshot } from "../src/types";

describe("AssistantCards", () => {
  it("把目的地和景点点击转换为结构化 Assistant Action", async () => {
    const snapshot = emptyAssistantSnapshot();
    snapshot.destination_candidates = [{
      candidate_id: "hangzhou",
      city: { name: "杭州" },
      score: 0.95,
      reasons: ["人文与自然丰富"],
    }];
    const destination = mount(AssistantCards, { props: { snapshot, busy: false } });

    await destination.get(".destination-grid .choice-card").trigger("click");
    expect(destination.emitted("select")?.[0]).toEqual([{
      kind: "select_destination",
      candidate_id: "hangzhou",
    }]);

    snapshot.requirements.destination = "杭州";
    snapshot.destination_candidates = [];
    snapshot.facts.catalog = {
      attractions: [{
        id: "west-lake",
        name: "西湖",
        address: "杭州市西湖区",
        category: "attraction",
        latitude: 30.25,
        longitude: 120.14,
      }],
      restaurants: [],
      hotels: [],
    };
    await destination.setProps({ snapshot: { ...snapshot } });
    await destination.get(".poi-grid .choice-card").trigger("click");
    await destination.get(".compact-action").trigger("click");

    expect(destination.emitted("select")?.[1]).toEqual([{
      kind: "select_attractions",
      attraction_ids: ["west-lake"],
    }]);
  });

  it("图片加载失败时显示可读占位，而不是破碎图片图标", async () => {
    const snapshot = emptyAssistantSnapshot();
    snapshot.requirements.destination = "杭州";
    snapshot.facts.catalog = {
      attractions: [{
        id: "west-lake",
        name: "西湖",
        address: "杭州市西湖区",
        category: "attraction",
        latitude: 30.25,
        longitude: 120.14,
        image_url: "https://invalid.example/west-lake.jpg",
      }],
      restaurants: [],
      hotels: [],
    };

    const wrapper = mount(AssistantCards, { props: { snapshot, busy: false } });
    await wrapper.get(".poi-grid img").trigger("error");

    expect(wrapper.get(".media-placeholder").text()).toContain("暂无图片");
  });
});
