# 飞书交易助手 (Feishu Trading Assistant)

一个跑在 VPS 上的飞书机器人。它读取你的 OKX 账户，监控仓位，定时发简报，并在私聊和群里回答你的行情问题。

这是第一步的版本，只读取，不下单。代码里没有任何下单、撤单、划转的功能。“经你确认后下单”是第二步，会单独开发，并且先在模拟盘上测试。

## 它现在能做什么

| 功能 | 你的私聊 | 群里 |
| --- | --- | --- |
| 开仓、加仓、减仓、平仓、止盈止损变动的提醒 | 有，含数量和盈亏 | 有，内容一样 |
| 风险提醒（保证金率偏低、离强平价近、当日亏损到线） | 有 | 有 |
| 读不到 OKX 时的故障提醒 | 有 | 不发（只和你有关） |
| 盘前简报、收盘小结 | 有，结合你的持仓，带“今日英语” | 有，同一份 |
| 每周复盘 | 有 | 有，同一份 |
| 发“仓位”看持仓 | 完整账户信息 | 你 @机器人 发，同样是完整账户信息 |
| 问行情，例如“现在黄金怎么看” | 可以，AI 会结合你的持仓 | 你 @机器人 问，AI 同样会结合你的持仓 |
| 问题前加“深度”，换更强的模型 | 可以 | 你可以 |

机器人只响应你本人的指令。群里其他人 @ 它，只会得到一句“我只响应主人的指令”。自动的提醒和简报照常发到群里，大家都看得到。

这两条规则在 `.env` 里可以改：

- `GROUP_SHOW_PNL=false`：群里不再显示资金和盈亏金额，只显示品种、方向和价位，AI 在群里也不再看你的账户，风险提醒和周复盘只发给你。
- `GROUP_OWNER_ONLY=false`：群里的其他人也可以 @机器人 问行情（每天有次数上限）。

账户数据只会出现在你的私聊和你登记的那一个群里。机器人被拉进别的群，不会在那里显示任何账户信息。

AI 只做分析和提醒，讲多空两边的理由和关键价位，不给“现在买入”这类指令。它只看得到 OKX 的价格和按 K 线算出的指标，看不到新闻和经济数据。

日常分析和深度分析的请求开启了 Claude 的备用模型选项 (server-side fallback)：如果主模型拒绝回答，接口会自动换一个备用模型重答，所以个别回答可能来自备用模型。

## 你需要准备的东西

1. 一台专门跑机器人的 VPS（云服务器）。Windows 和 Ubuntu 都可以，下面两种都有步骤。只接 OKX 的话，Ubuntu 就够用，也更便宜。以后要接 MT5，需要 Windows。这台机器不要和别的用途共用，因为 OKX 的 key 会绑定它的 IP。机房地区要选你的 OKX 账户允许使用的地区。
2. GitHub 上的私有仓库 `feishu-trading-bot`（代码放这里）。
3. 飞书开放平台上的一个企业自建应用（下面有步骤）。
4. OKX 的 API key、Secret key 和 Passphrase（你已经有了）。
5. Claude 的 API key。

密钥和密码的规则只有一条：OKX、飞书、Claude 的密钥只填进 VPS 上的 `.env` 文件，服务器的登录密码只留在你自己手里。都不要发到任何聊天里（包括发给 Claude），不要截图，不要上传到 GitHub。

## 第一步：在飞书开放平台创建机器人

在电脑浏览器里打开 https://open.feishu.cn/app ，用你自己的飞书账号登录。菜单的名字以后可能略有变化，找意思相近的即可。

1. 点“创建企业自建应用”，名字可以叫“交易助手”。
2. 进入应用，在“添加应用能力”里添加“机器人”。
3. 在“凭证与基础信息”里能看到 App ID 和 App Secret。先不用复制，等会儿在 VPS 上填。
4. 在“权限管理”里，把下面四个权限逐个搜索出来并开通（直接把英文代码粘进搜索框）：
   - `im:message`
   - `im:message.p2p_msg:readonly`
   - `im:message.group_at_msg:readonly`
   - `im:message:send_as_bot`
5. “事件与回调”这一步先放着，等机器人在 VPS 上跑起来以后再回来做（见第四步的第 3 点）。

## 第二步：在 VPS 上安装

按你的系统选一种。

### Windows

用远程桌面登录 VPS，以下操作都在 VPS 上做。

1. 安装 Python 3.12：到 https://www.python.org/downloads/windows/ 下载 3.12 的 Windows installer (64-bit)。安装时务必勾选“Add python.exe to PATH”。我在 3.12 和 3.13 上测过，其他版本没有测。
2. 安装 Git：到 https://git-scm.com/download/win 下载安装，一路默认即可。
3. 打开“命令提示符”(Command Prompt)，依次输入下面两行。第二行里的 `你的用户名` 换成你的 GitHub 用户名。第一次会弹出 GitHub 的登录窗口，登录即可。

   ```
   cd C:\
   git clone https://github.com/你的用户名/feishu-trading-bot.git
   ```

4. 打开 `C:\feishu-trading-bot\scripts` 文件夹，双击 `setup.bat`。它会安装需要的组件，并生成一个空的 `.env` 文件。看到 `[OK] Setup finished` 就是成功了。

### Ubuntu

用 SSH 登录服务器（Ubuntu 24.04 自带 Python 3.12，不用另装）。

1. 刚登录时是 root 用户。输入下面三行，安装基础组件，并建一个专门跑机器人的普通用户 `bot`，然后切换到它：

   ```
   apt update && apt install -y python3-venv git gh
   adduser --disabled-password --gecos "" bot
   su - bot
   ```

2. 登录 GitHub 并下载代码。把 `你的用户名` 换成你的 GitHub 用户名：

   ```
   gh auth login
   gh repo clone 你的用户名/feishu-trading-bot
   cd feishu-trading-bot
   bash scripts/setup.sh
   ```

   `gh auth login` 会问几个问题，依次选 GitHub.com、HTTPS、Yes、Login with a web browser。它会显示一个一次性验证码，在你自己电脑的浏览器里打开 https://github.com/login/device ，输入验证码即可。看到 `[OK] Setup finished` 就是安装成功了。

## 第三步：填写 .env

- Windows：用记事本打开 `C:\feishu-trading-bot\.env`。
- Ubuntu：在 `feishu-trading-bot` 文件夹里输入 `nano .env`。改完按 Ctrl+O 再按回车保存，按 Ctrl+X 退出。

在等号后面填值，等号两边不要加空格和引号。这一轮先填这些：

- `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`：飞书应用的“凭证与基础信息”页面。
- `OKX_API_KEY`、`OKX_SECRET_KEY`、`OKX_PASSPHRASE`：你现有的那把 OKX key。
- `ANTHROPIC_API_KEY`：Claude 的 API key。

`OWNER_OPEN_ID` 和 `GROUP_CHAT_ID` 先空着，第四步会拿到。其余的项都有默认值，文件里每一项上面都有中文说明。

## 第四步：自检、启动、登记

1. 自检 (self-check)。它会逐项检查配置、OKX、Claude 和飞书，`[OK]` 是通过，`[X]` 是没通过并附原因，`[!]` 是提醒。这一轮出现“还没填 OWNER_OPEN_ID”的提醒是正常的。如果飞书这一项暂时没通过，先接着做下面几步，等应用发布以后再自检一次。
   - Windows：双击 `scripts\selfcheck.bat`。
   - Ubuntu：输入 `.venv/bin/python -m bot selfcheck`。
2. 启动机器人。
   - Windows：双击 `scripts\start.bat`。这个黑色窗口要一直开着。
   - Ubuntu：先输入 `exit` 回到 root 用户，再输入下面这一行。它把机器人装成系统服务，在后台运行，掉线会自动重启，服务器重启后也会自己启动。

     ```
     bash /home/bot/feishu-trading-bot/scripts/install-service.sh
     ```

3. 回到飞书开放平台，做“事件与回调”的配置：
   - 订阅方式选“使用长连接接收事件”并保存。如果提示应用还没有建立长连接，说明机器人没在运行，回到上一步确认它已经启动。
   - 点“添加事件”，搜索 `im.message.receive_v1`（接收消息）并添加。
4. 在“版本管理与发布”里创建一个版本并发布。可用范围要包含你自己。发布之后权限和事件才生效。
5. 在飞书里搜索你的机器人，私聊它发送 `/whoami`。它会回你的 open_id，把它填进 `.env` 的 `OWNER_OPEN_ID`。
6. 建一个群，把朋友拉进来，再在群设置的“群机器人”里添加你的机器人。在群里 @机器人 发送 `/whoami`，把它回的 chat_id 填进 `.env` 的 `GROUP_CHAT_ID`。
7. 重启机器人，让新填的两项生效。你会在私聊里收到“交易助手已上线”的卡片，里面有当前的账户和持仓。
   - Windows：关掉黑色窗口，重新双击 `scripts\start.bat`。
   - Ubuntu：以 root 用户输入 `systemctl restart feishu-trading-bot`。

关于朋友：机器人是你组织里的自建应用，最稳妥的做法是把朋友邀请成为你飞书组织（团队）的成员，再拉进群。如果朋友留在组织外面，机器人能不能在外部群里使用，取决于飞书对应用“对外共享”的规定，这一点我没有实测过。

## 第五步：把 OKX 的 key 绑定到 VPS 的 IP

在 OKX 的 API 管理页面编辑这把 key，把“绑定 IP 地址”填成 VPS 的公网 IP（在 VPS 服务商的控制台里能看到）。这样即使 key 泄露，别的机器也用不了。另外，OKX 官方说明里写着，带交易权限但没有绑定 IP 的 key，闲置 14 天会被自动删除。

提币权限保持关闭。

## 日常使用

- 发“仓位”：看账户和持仓。私聊里直接发，群里要 @机器人。
- 直接提问：例如“现在黄金怎么看”。
- 在问题前加“深度”：例如“深度 下周黄金怎么看”，会换更强的模型。
- 发“帮助”：看说明。
- 发 `/whoami`：看自己的 open_id 和当前会话的 chat_id。

定时简报的时间、提醒线、群里的规则，都在 `.env` 里改。改完要重启机器人。

## 让它一直运行

### Windows

平时：`start.bat` 的窗口开着就行。断开远程桌面时直接关掉远程桌面的窗口，不要选“注销”(sign out)，否则机器人会跟着停。机器人如果意外退出，窗口会在 15 秒后自动重新启动它。

VPS 重启后自动启动（可选）：以管理员身份打开命令提示符，输入下面这一行。之后 VPS 每次开机都会在后台启动机器人，没有窗口，运行情况看 `logs\bot.log`。

```
schtasks /Create /TN "FeishuTradingBot" /TR "C:\feishu-trading-bot\scripts\start.bat" /SC ONSTART /RU SYSTEM /F
```

用了开机自启以后，就不要再手动双击 `start.bat`，否则会有两个机器人同时回话。取消自启的命令是 `schtasks /Delete /TN "FeishuTradingBot" /F`。

### Ubuntu

第四步里的 `install-service.sh` 已经把它装成了系统服务，不需要再做什么。常用命令（以 root 用户）：

```
systemctl status feishu-trading-bot     看它是否在运行
systemctl restart feishu-trading-bot    重启（改了 .env 之后要做）
systemctl stop feishu-trading-bot       停止
tail -n 50 /home/bot/feishu-trading-bot/logs/bot.log    看最近的运行记录
```

### 哪些部分我实测过

Ubuntu 的 `setup.sh` 我在 Linux 上实际跑通过，生成的服务文件也通过了 systemd 的格式检查。没有实测的部分：服务在真实服务器上的启动、`gh` 登录这一步，以及 Windows 的 `.bat` 脚本和 schtasks 命令（我没有 Windows 机器）。第一次用的时候如果报错，把报错文字发我，注意别带上密钥。

## 更新

- Windows：双击 `scripts\update.bat`，然后关掉机器人窗口，重新双击 `start.bat`。
- Ubuntu：以 `bot` 用户在项目文件夹里输入 `bash scripts/update.sh`，再以 root 用户输入 `systemctl restart feishu-trading-bot`。

## 常见问题

- 自检里 OKX 行情读取失败：这台机器连不上 OKX。先确认机房地区是你的 OKX 账户允许使用的地区。
- 自检里 OKX 账户读取失败：多半是 key 填错、Passphrase 不对，或者这把 key 绑定的 IP 不是这台 VPS。
- 自检里提示时间相差好几秒：打开系统的自动校时。
- 飞书里发消息没反应：检查应用是否已发布、事件 `im.message.receive_v1` 是否已添加、四个权限是否都开通。群里要 @机器人 才会回答，而且默认只回答你本人。
- Windows 的黑色窗口里中文显示成方块：不影响运行。可以在窗口标题栏上点右键，属性，字体，换成“新宋体”(NSimSun)。
- 想看运行记录：`logs` 文件夹里的 `bot.log`。交易记录和账户快照在 `data` 文件夹里的 `bot.db`。

## 代码结构（给以后维护的人看）

```
bot/config.py         读取 .env，检查配置
bot/okx_client.py     OKX 只读客户端（签名、行情、账户、持仓、止盈止损）
bot/models.py         数据结构
bot/indicators.py     EMA、RSI、ATR
bot/market.py         把行情整理成给 AI 看的事实清单
bot/monitor.py        仓位变化检测和风险规则（纯规则，不用 AI）
bot/journal.py        SQLite 交易日志和账户快照
bot/analyst.py        调用 Claude：Haiku 分类、Sonnet 日常、Opus 深度
bot/reports.py        文字排版，含金额的完整版和只有价位的简版
bot/service.py        决定什么发给你、什么发到群
bot/router.py         处理收到的消息，执行“只认主人”和账户可见范围的规则
bot/scheduler.py      定时任务
bot/feishu_client.py  飞书收发消息（长连接）
bot/app.py            把各部分接起来
bot/__main__.py       命令行入口：run、selfcheck、status、brief
scripts/              Windows 的 .bat 和 Linux 的 .sh 安装、启动、更新脚本
tests/                自动测试，运行方式：python -m pytest
```

开发时安装测试依赖：`pip install -r requirements-dev.txt`。

## 英语角 (English corner)

| 中文 | English | 例句 |
| --- | --- | --- |
| 仓位、持仓 | position | I have a long position in gold. |
| 开仓 / 平仓 | open / close a position | I closed my position before the news. |
| 加仓 / 减仓 | add to / reduce a position | I reduced my position by half. |
| 止损 / 止盈 | stop-loss / take-profit | Where is your stop-loss? |
| 保证金率 | margin ratio | My margin ratio is getting low. |
| 强平价 | liquidation price | The price is far from my liquidation price. |
| 浮动盈亏 | unrealized PnL | My unrealized PnL is positive today. |
| 盘前简报 | pre-market brief | Did you read the pre-market brief? |
| 自检 | self-check | Run the self-check first. |
| 模拟盘 | demo trading | Test it in demo trading before going live. |
| 系统服务 | service | The bot runs as a service on the server. |
