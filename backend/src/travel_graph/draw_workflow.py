"""生成中文版 TravelGraph 流程图，不修改实际工作流。"""

from pathlib import Path

from travel_graph.application import travel


def chinese_mermaid() -> str:
    """读取已编译的真实 Graph，并只替换展示文本。"""

    mermaid = str(travel.get_graph().draw_mermaid())
    labels = {
        "__start__([<p>__start__</p>]):::first": '__start__(["开始"]):::first',
        "validate_order(validate_order)": 'validate_order["校验旅行工单"]',
        "build_itinerary(build_itinerary)": 'build_itinerary["生成每日行程"]',
        "build_routes(build_routes)": 'build_routes["查询景点路线"]',
        "calculate_budget(calculate_budget)": 'calculate_budget["计算旅行预算"]',
        "validate_plan(validate_plan)": 'validate_plan["校验最终规划"]',
        "route_preview(route_preview)": 'route_preview["路线预览与用户确认"]',
        "save_trip(save_trip)": 'save_trip["幂等保存行程"]',
        "__end__([<p>__end__</p>]):::last": '__end__(["结束"]):::last',
        "&nbsp;completed&nbsp;": "已完成",
        "&nbsp;active&nbsp;": "进行中",
        "&nbsp;revise&nbsp;": "修改行程",
        "&nbsp;save&nbsp;": "确认保存",
    }
    for source, target in labels.items():
        mermaid = mermaid.replace(source, target)
    return mermaid


def main() -> None:
    """在当前文件夹生成 Mermaid 源文件和 Markdown 预览文件。"""

    output_dir = Path(__file__).resolve().parent
    mermaid = chinese_mermaid()
    mmd_path = output_dir / "travel_workflow_cn.mmd"
    md_path = output_dir / "travel_workflow_cn.md"

    mmd_path.write_text(mermaid, encoding="utf-8")
    fence = chr(96) * 3
    md_path.write_text(
        "# OpenZLTravel 后端工作流\n\n" + fence + "mermaid\n"
        + mermaid
        + "\n" + fence + "\n",
        encoding="utf-8",
    )
    print(f"已生成 Mermaid：{mmd_path}")
    print(f"已生成 Markdown：{md_path}")


if __name__ == "__main__":
    main()
