import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import AssistantMarkdown from "../src/features/assistant/AssistantMarkdown.vue";

describe("AssistantMarkdown", () => {
  it("渲染加粗和表格而不显示 Markdown 标记", () => {
    const wrapper = mount(AssistantMarkdown, {
      props: {
        content: "**行程已确认**\n\n| 日期 | 安排 |\n| --- | --- |\n| 8/30 | 西湖 |",
      },
    });

    expect(wrapper.find("strong").text()).toBe("行程已确认");
    expect(wrapper.find("table").exists()).toBe(true);
    expect(wrapper.text()).not.toContain("**");
  });

  it("清理模型输出中的可执行 HTML", () => {
    const wrapper = mount(AssistantMarkdown, {
      props: { content: '<img src="x" onerror="alert(1)"><script>alert(2)</script>' },
    });

    expect(wrapper.html()).not.toContain("onerror");
    expect(wrapper.find("script").exists()).toBe(false);
  });
});
