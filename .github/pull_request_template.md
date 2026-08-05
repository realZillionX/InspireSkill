## Summary

-

## Validation

- [ ] `cd cli && uv run pytest -q`
- [ ] `cd cli && uv run ruff check inspire tests`
- [ ] `cd cli && uv run mypy`
- [ ] `cd cli && uv build`

## Checklist

- [ ] 变更范围与 PR 目标一致，没有引入无关格式化或重构。
- [ ] 文档、help 文本或模板中的中文标点和中英文空格符合项目约定。
- [ ] 若触碰 live Inspire 平台资源，已说明创建对象、清理状态和残留风险。
- [ ] 若改变 CLI 行为，已补充或更新测试。
