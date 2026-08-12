import { describe, expect, it } from "vitest";

import { errorMessage } from "../src/api";

describe("errorMessage", () => {
  it("优先展示后端返回的中文错误", () => {
    const error = {
      isAxiosError: true,
      response: { data: { error: { message: "高德地图服务暂时不可用" } } },
    };

    expect(errorMessage(error)).toBe("高德地图服务暂时不可用");
  });

  it("未知错误使用通用提示", () => {
    expect(errorMessage(new Error("network"))).toBe("操作失败，请稍后重试。");
  });
});
