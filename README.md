# 古道分时通行的离线额度交还

本目录提供现场联调用的合成合作方记录。记录中的机构、地点、旅客和标识均为虚构。每份记录可单独使用。

## 合作方报文

`windows` 指定现场许可的人数，时间使用营运日开始后的整数秒，左闭右开。`gates` 的 `error_bound` 表示该门的可信绝对时钟误差，`max_offline` 是自最后可信对时起可离线工作的秒数。回报的 `clock_error` 可包含失准观测，不能作为偏差超界时继续放行的证明。申请的团体 `count` 是实际入段人数，一个通行凭证绑定同一团体、人数、入口、方向及窗口。
`grant_id` 引用分配给闸口的授权额度，`permit` 为在现场展示的通行凭证标识。`use_no` 是该闸口在该授权下的本地核验记录编号。`last_use_no` 是闸口已停止使用所交还额度时最后一个记录编号，记录到齐需要核对连续性。`reported_consumed` 是现场报出的扣减人数，不替代独立核验记录。临时停开使用 `closure` 发布，`closure_seen` 是看到通知，`closed` 是实际停止接受指定方向的通行权。

## 本地联调

使用 Python 3.11 的标准库。现场联调环境固定为 Python 3.11.2。启动本地资料端后保持该进程运行，默认只监听回环地址的 8090 端口。

```sh
python partner.py fixtures/two-offline-gates.json
```

资料端按路径回放记录中的响应。接口 `GET /fixture/facts` 返回起始事实，`GET /fixture/events` 返回按到达时间排列的原始事件。同一到达时刻的事件按记录中的顺序出现。`GET /fixture/journal` 返回收到的合作方请求，含未匹配及已收件但主动断开连接的请求。重启资料端会从该份记录开头开始。

`exchanges` 每条只使用一次，按方法、路径和列出的正文键值及请求头匹配最早尚未使用的记录。路径中的花括号匹配一个标识段。`delay_ms` 是合作方回包延迟，`drop` 表示合作方收到请求后直接关闭连接。未匹配的请求返回 404。记录字段可随新的联调资料调整。

在同一目录用 `python -m unittest -v` 核对资料载入和合作方接口，包括已记录的状态码、回包、断连及接收记录。

## 通行额度服务与本地核验程序

`trail/` 在上述字段基础上实现两段程序，供各闸口值班机实际接入，全程只依赖 Python 3.11 标准库。

### 模块

| 模块 | 职责 |
| --- | --- |
| `trail/model.py` | 窗口、闸机、授权额度（grant）、通行凭证、停开三态等枚举与值对象 |
| `trail/service.py` | 额度服务：分配、跨闸借出、凭证整笔扣减、改时确认、交还对账、运营视图 |
| `trail/verifier.py` | 闸口本地核验程序：sqlite 持久化、断网扫证、时钟/离线界判定、重启防重放 |
| `trail/httpapi.py` / `trail/client.py` | 额度服务的回环 HTTP 接口与值班机客户端 |
| `trail/driver.py` | 读取合作方资料（本地文件或运行中的资料端）回放事件并产出对账报告 |
| `trail/serve.py` | 额度服务进程入口 |

### 关键规则

* **额度绑定窗口与方向**：团体 `count` 整笔扣在对应 `grant_id` 上；同窗口内闲置额度可经 `lend` 借给排队闸口，来源授权保留 `earmarked` 血缘，窗口容量不重复计算。
* **离线权按闸口与时间窗生效**：凭证只在授权闸口、有效窗口（计入 `error_bound` 保守判定）内可用；时钟观测误差超界或离线超过 `max_offline` 的闸口立即停止离线放行。
* **改时/换入口**：旧凭证仍可能在现场使用时，`move` 只能返回 `pending`；只有旧闸离线权确实停止（时钟超界、已 `closed`、或在线可送达吊销）才 `confirmed`，旧证作废、新证整笔承接。
* **重启不重复放行**：核验程序把放行事实写入本机 sqlite（`use_record` 按 grant 连续编号），重启后重扫同一凭证命中 `already_used`。
* **交还与停开分离**：停开通知保留 `published → seen → closed` 三态；交还按「安全收回 / `unaccounted` 敞口 / 已划转结算」三类记账，可能已放行却无回报的额度进 shadow，不得挪用。
* **对账**：`use_no` 连续性到齐、无未关闭的 active 凭证后标记 `reconciled`；`reported_consumed` 与独立核验记录不符抛 `ReconcileError`；跨窗口放行各自独立核算。
* **运营视图**：`GET /service/overview` 与 `GET /service/gates/{gate}` 给出每入口的放行（released）、shadow、可调剂（reclaimable）、窗口余量（free_capacity）及无法安全履约的 stranded 凭证。

### 本地联调

```sh
# 1) 直接回放资料文件（进程内服务 + 各闸口 sqlite）
python -m trail.driver fixtures/two-offline-gates.json --db-dir tmp/gates
python -m trail.driver fixtures/uncertain-clock.json --db-dir tmp/gates

# 2) 对接运行中的资料端（先按上文启动 partner.py）
python -m trail.driver fixtures/two-offline-gates.json \
  --partner http://127.0.0.1:8090 --db-dir tmp/gates

# 3) 单独启动额度服务（默认回环 8091）
python -m trail.serve --facts fixtures/two-offline-gates.json --port 8091
```

回放报告含每条事件的处置（扫证允许/拒绝原因、改时状态、交还分类、对账结果）、八项不变量核对结果与运营总览；任一不变量失败时进程返回非零。`python -m unittest -v` 覆盖服务账本、本地核验、HTTP 接口与端到端联调共 37 个用例。
