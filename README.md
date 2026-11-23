# cc-clinic-lab · 临床试验访视采集台

数据工程产品线基线。题号前缀 `clinic-*`，默认端口 **7600**。

## 技术栈

- 后端：CherryPy + SQLite
- 前端：原生 JS SPA（顶栏薄荷绿丝带导航）
- 会话：itsdangerous 签名 Cookie
- 测试：pytest + httpx（依赖已列入 requirements）

## 登录

- 地址：`http://127.0.0.1:7600/login`
- 账号：`admin` / `123456`

## 页面

| 页面 | 能力 |
|------|------|
| 试验中心 | 中心登记、启停/观察、配额、方案版本生效 |
| 访视批次 | 开批、模拟抓取、追加访视行、合格入库、收齐/关闭 |
| 字段质控 | 按批/全量扫描、发现关闭/重开 |
| 缺口补采 | 排队、执行失败/重试；死信回炉落定（物化包落点行） |
| 访视对账 | 缺口/发现/补采对照、中心矩阵、审计 |
| 台参数 | 设置读写 |

## 主链

中心 → 访视批次抓取 → 质控发现 → 缺口补采 → 对账报告；设置与审计贯穿写操作。

## 死信回炉（甲路：物化包 / cutover_book）

- 死信 = 失败补采任务；回炉落定把当刻**全部死信**抄成 `dead_letter_reflow_rows` 落点行，并写 `dead_letter_reflows` 台账。
- 落定同时把读轨钉到 `cutover_book`、锁 64 位指纹、置 `reject_refresh`；同一批死信不得回刷。
- 徽标由「真实落点行 + 死信条数」推导：落定后又冒出死信则徽标转红（面板与死信条数拧花），并在重新落定前挡住补采排队。
- 读接口（仪表盘 / 名录 / 详情 / 状态）一律只读，不写库；两侧并发落定由 `BEGIN IMMEDIATE` 串行化，只有一侧成功。
- 只亮面板不写落点行会被验收驳回。

## 本地

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

Docker：`docker compose up -d --build`，健康检查打 `/login`。

## 后续题面预留切口

- 访视行跨中心去重与冲突裁决
- 方案换版后存量批次字段兼容扫描
- 补采分片与并发上限
- 对账差异工单与导出
