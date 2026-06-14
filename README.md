# A股重点板块热度记录器

这是一个静态自动化复盘项目：GitHub Actions 在 A 股收盘后抓取行情、解析重点板块、计算热度指标和状态，随后生成 `docs/` 目录下的 GitHub Pages 静态网页、`latest.json`、`history.json` 与 `history.csv`。网页打开时只读取已经生成好的静态文件，不实时请求行情接口，也不依赖服务端 API。

本项目只用于板块热度记录、复盘和观察，不提供买入或卖出建议。

## 输出

- GitHub Pages 手机友好网页：今日摘要、关注板块表、强势榜、过热榜、低位榜、趋势图、历史归档。
- Telegram 每日摘要：重点板块变化、异常提示和网页链接。
- CSV/JSON 历史数据：用于复盘和后续重新计算。

## 板块配置

编辑 `boards.yml`。每个板块支持：

- `enabled`：是否启用。
- `priority`：展示排序，数字越小越靠前。
- `note`：板块说明。
- `provider_board`：标准行业或概念板块来源，当前优先支持东方财富板块代码。
- `include`：在标准板块基础上手动加入股票。
- `exclude`：在标准板块基础上手动剔除股票。
- `custom_members`：自定义股票池。没有标准板块时，可用它构建完全自定义板块。

重要板块建议配置 `include` 或 `custom_members` 作为手动兜底。这样即使标准板块成分股接口临时失败，系统仍可用这些股票生成最小可用复盘数据，避免页面完全空白。

示例：

```yaml
boards:
  - id: ai_compute
    name: AI算力
    enabled: true
    priority: 10
    note: 重点观察算力基础设施。
    provider_board:
      source: eastmoney
      code: BK1137
      type: concept
      name: 算力概念
    include:
      - 300308.SZ
    exclude:
      - 000000.SZ
    custom_members: []
```

每日会保存实际参与计算的成分股快照到 `docs/data/members/日期.json`，保证历史复盘口径可追溯。

## 指标口径

- 日报主指标：有效成分股等权涨跌幅。
- 辅助指标：市值加权涨跌幅、上涨占比、成交额相对 20 日均值、有效报价覆盖率、数据源原始板块涨跌幅。
- 停牌、无报价、异常缺失股票不参与主计算，但计入覆盖率说明。
- 覆盖率低于 80% 或成分股数量较昨日变化超过 20% 的板块，不参与强弱、过热、低位和高低切换判断。

状态包括：强势延续、过热警戒、调整中、低位观察、高低切换候选。状态判断会使用近 5 日、20 日、60 日表现、历史位置、连续涨跌天数和量能放大倍数，不只看单日涨跌。

## 自动化

GitHub Actions 使用 UTC cron：

- `35 7 * * 1-5`：北京时间 15:35。
- `10 8 * * 1-5`：北京时间 16:10 校准。

手动触发或定时运行时，如果当天不是交易日，或行情源最新数据仍停留在上一交易日，脚本会正常退出并在 `latest.json` 标记 `non_trading_day` 或 `stale_ok`，页面继续展示上一交易日有效数据，不会把它当作失败覆盖。

同时支持 `workflow_dispatch` 手动触发。GitHub Pages 使用分支目录部署：

- 进入仓库 `Settings` -> `Pages`。
- `Source` 选择 `Deploy from a branch`。
- `Branch` 选择 `main`。
- `Folder` 选择 `/docs`。

Telegram 推送需要配置仓库 Secrets：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## 本地运行

```bash
python -m unittest discover -s tests
python scripts/build_static.py --config boards.yml --output docs --history-days 30 --fixture
```

去掉 `--fixture` 后会尝试请求真实行情源。
