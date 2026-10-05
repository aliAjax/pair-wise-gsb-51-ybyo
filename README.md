# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/guarantees.py`：代偿批次状态、额度/回款校验与回款冲减分配规则。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：纾困记录、共享额度、代偿批次与追偿回款演示页面。
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
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。方案失效动作为`default`。

### 担保代偿与追偿

几家担保机构共用年度代偿额度池。代偿批次状态：`pending`（提交即预占、待复核）、`confirmed`（复核确认，占用生效）、`rejected`（驳回释放）、`voided`（方案失效作废释放）。

- `POST /api/guarantors`：登记担保机构，数据 `{"code":"G01","name":"..."}`。
- `GET /api/guarantors`：担保机构列表。
- `POST /api/quotas`：配置/调整年度共享额度，数据 `{"year":2026,"total_quota":1000000}`。
- `GET /api/quotas?year=2026`：额度池占用（待复核预占/已确认占用/剩余可用）。
- `POST /api/batches`：提交代偿并预占额度，数据 `{"batch_no":"B-001","guarantor_code":"G01","year":2026,"amount":300000,"record_id":1,"note":""}`。额度不足返回409；同批次号重试按原批次返回（`idempotent_hit:true`），不重复占用。
- `GET /api/batches?year=&status=&guarantor_code=&record_id=`：批次列表，含代偿额、已追偿、剩余差额。
- `GET /api/batches/{id}`：批次详情。
- `POST /api/batches/{id}/review`：复核，数据 `{"approved":true,"review_note":"..."}`；仅`pending`可复核。
- `POST /api/batches/{id}/recoveries`：登记追偿回款，数据 `{"serial_no":"TX-1","amount":120000}`。仅`confirmed`批次可登记；同一流水号只匹配一笔回款（重复返回原记录，不重复冲减）；不足留差额，超额时`refunded_amount`为退回金额、`remaining_after`为冲减后剩余。
- `GET /api/batches/{id}/recoveries`、`GET /api/recoveries?batch_id=`：回款列表。
- `GET /api/stats?year=2026`：纾困记录状态统计 + 担保统计（批次状态分布、回款冲减与退回、各机构占用/已追偿/剩余差额）。

并发与联动规则：

- 提交代偿在`BEGIN IMMEDIATE`事务内完成额度校验与批次写入，两人同时抢同一额度时先到者成功，后来者收到409，无超卖。
- 纾困方案执行`default`失效后，关联的未确认（`pending`）批次同事务自动作废并释放预占，额度按批次状态重算；已确认批次及其回款不受影响。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。担保相关角色：`guarantee_admin`（机构与额度配置）、`guarantor_officer`（提交代偿、登记回款）、`guarantor_reviewer`（复核确认/驳回）；`admin`拥有全部权限。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及额度预占/确认/驳回、回款冲减与重复流水、并发先到者得、方案失效批次作废和统计。
