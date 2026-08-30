# 显示面板论文日报 · 云端推送机器人

电脑不开机, 也能每天早上 8 点收到显示面板行业新论文的微信推送。

**原理**: GitHub Actions 云端定时(00:10 UTC = 北京时间 08:10) → Python 脚本查询 OpenAlex
(覆盖 SID 2025 全部参与机构, 排除 SID/ICDT/IMID 会议论文) → DOI 去重 → 12 大技术方向分类 +
质量评级 → 完整日报存入 `reports/` → 摘要经 Server酱 推送到微信「服务通知」。

## 部署步骤(一次性, 约 5 分钟)

### 1. 获取 OpenAlex API Key(免费, 推荐但非必须)

- 打开 https://openalex.org 注册账号(约 30 秒): 点首页右上角 **Get started** 或 **Log in**
- 注册/登录后, 直接访问 https://openalex.org/settings/api 复制 API Key
- 额度对比:
  - 无 key: 每天 $0.10 免费额度(约 1000 次 list/filter 调用)
  - 有 key: 每天 $1.00 免费额度(约 10000 次)
  - 本任务每天只需约 8 次 list/filter 调用, 花费约 $0.0008, **无 key 也够用**

如果你暂时不想注册, 可以先跳过这步, 第 3 步的 `OPENALEX_API_KEY` 不填即可。

### 2. 创建 GitHub 私有仓库并推送

```bash
cd display-papers-bot
git init
git add .
git commit -m "init: display papers daily bot"
git branch -M main
git remote add origin https://github.com/<你的用户名>/display-papers-bot.git
git push -u origin main
```

仓库建议设为 **Private**(私有)。

### 3. 配置仓库 Secrets

GitHub 仓库页面 → **Settings → Secrets and variables → Actions → New repository secret**, 添加两条:

| Secret 名称 | 值 |
|:--|:--|
| `SERVERCHAN_SENDKEY` | 你的 Server酱 SendKey(即本地 `~/.workbuddy/.serverchan_key` 中那串 `SCT...`) |
| `OPENALEX_API_KEY` | 第 1 步复制的 OpenAlex API Key |

### 4. 手动触发首次运行验证

仓库页面 → **Actions** 标签 → 左侧选 `Display Papers Daily` → 右侧 `Run workflow` 手动触发。
跑完几分钟后, 微信「服务通知」应收到当日论文日报(首次含近 3 天窗口的回溯论文, 属正常)。

## 日常运维

- 每天北京时间 **08:10** 自动执行(GitHub cron 可能有几分钟到十几分钟延迟, 属正常)
- 完整日报归档在仓库 `reports/display_papers_daily_YYYY-MM-DD.md`
- 去重状态 `seen.json` 由 Actions 自动 commit 回仓库, 换 runner 也不丢
- 增删监控机构: 编辑 `institutions.json`
- 调整推送时间: 编辑 `.github/workflows/daily.yml` 中的 cron(UTC 时区)
- 停止推送: Actions 页面 Disable workflow, 或直接删仓库

## 文件结构

```
papers.py                          # 主脚本(纯标准库, 无第三方依赖)
institutions.json                  # 监控机构关键词(源自 SID 2025 参与单位清单)
seen.json                          # 已推送论文去重库(自动维护)
reports/                           # 每日完整日报归档
.github/workflows/daily.yml        # GitHub Actions 定时工作流
```
