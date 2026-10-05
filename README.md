# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值、履约状态和冲突检查，以及担保代偿的额度预占/确认/作废与回款匹配规则。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计，含担保批次、回款与差额汇总及各年度额度池占用。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 担保代偿

多家担保机构共用年度代偿额度（可用额 = 总额 − 预占 − 已确认 + 已回款，全部由批次与回款流水推导重算）。代偿批次先预占额度，复核确认后才生效；记录进入终态（`cured`/`defaulted`）时，该记录未确认的批次在同一事务内作废并释放预占。批次号与回款流水号均为幂等键：写入失败后按原批次/原流水重试会返回原结果，不重复占用、不重复冲减。额度占用在同一事务内检查，并发提交同一额度时先到者成功。

- `GET /api/quota-pools`：年度额度池列表（预占、确认、回款、可用）。
- `POST /api/quota-pools`：创建年度额度池（仅管理员），请求体`{"year":2026,"total_amount":1000000}`。
- `GET /api/agencies`、`POST /api/agencies`：担保机构查询与登记（登记仅管理员），请求体`{"code":"GA01","name":"..."}`。
- `POST /api/records/{id}/compensations`：提交代偿批次并预占额度（`guarantee_officer`），请求体`{"batch_no":"...","agency_code":"...","amount":123,"year":2026}`（`year`可缺省为当年）；记录需处于`active`或`defaulted`。
- `GET /api/records/{id}/compensations`、`GET /api/compensations`、`GET /api/compensations/{id}`：批次列表与详情（含回款明细与差额）。
- `POST /api/compensations/{id}/confirm`：复核确认（`underwriter`），预占转为有效占用；已作废批次不能确认。
- `POST /api/compensations/{id}/recoveries`：登记追偿回款（`guarantee_officer`），请求体`{"flow_no":"...","amount":123}`；仅已确认批次可登记，同一流水只匹配一笔回款，不足留差额，超额部分记为退回并显示剩余。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。担保相关角色：`guarantee_officer`（提交批次、登记回款）、`underwriter`（复核确认）、`admin`（额度池与机构维护）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
