# 七个环境变量的故事

程序跑通那晚我挺开心的。在飞书里发条消息，2 秒回复，上下文连续。我满意地关掉终端去睡觉。

第二天早上打开 Mac，发现 bot 没了。

对，关掉终端 = 杀掉进程。我需要让它自己活着——开机自动运行，挂了自动重启。在 Mac 上，干这事的叫 **launchd**。

---

## launchd 是什么

如果你用过 Windows，launchd 大概相当于"服务管理器"。它是 macOS 启动后的第一个进程（PID 1），所有后台服务都归它管。

你想让一个程序开机自启，只要写一个 `.plist` 配置文件（XML 格式的），告诉 launchd："这个程序在这，帮我看着它，挂了就重启。"然后用 `launchctl load` 加载就行。

听起来很美。我满怀信心地写好 plist，加载，给 bot 发了条消息——

没反应。

---

## 花式报错 Parade

看日志：`command not found`。哦，launchd 的 PATH 里没有我装命令的路径。加上。

再来：bot 起来了，但两秒后又死了。日志里花花绿绿全是错——**403 Forbidden**、**command not found**、**permission denied**。像一场报错灯光秀，我坐在那看得目瞪口呆。

permission denied 好理解，文件权限没给。command not found 也好理解，PATH 没配全。但那个 **403** 是什么鬼？

我的 API key 明明是对的啊。

---

## 403：排查了将近一小时

我做了这些排查：

1. 把 API key 复制出来，用 curl 直接调——正常，200
2. 在终端里直接 `python3 ws_bot.py`——正常，能连通
3. 通过 launchd 启动——403

那天晚上我从各个角度怀疑了一遍：key 过期了？账号被封了？飞书 IP 被拉黑了？我甚至登上 Claude 官网手动试了 API——没问题啊。

一个小时后我终于想通了。

**不是认证问题，是网络问题。**

我在国内。Claude 的 API 服务器在海外。我平时在终端里能调通，是因为 shell profile 里配了 `http_proxy` 和 `https_proxy`——所有请求自动走代理。

但 launchd 启动的进程，**不会继承你终端里的任何环境变量**。它的环境干净得像一张白纸——没有 PATH，没有 HOME，当然也没有 `http_proxy`。

请求没走代理，直连海外 IP，被墙了。而被墙的表现恰好就是 403（不是超时，不是连接拒绝，是直接返回 403）。这也是为什么我一开始怎么都没往代理方向想——403 太像认证问题了。

想通那一刻我盯着屏幕愣了三十秒。然后在 plist 里加了两行环境变量：

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>http_proxy</key>
    <string>socks5://127.0.0.1:7890</string>
    <key>https_proxy</key>
    <string>socks5://127.0.0.1:7890</string>
</dict>
```

重新 load。Claude API 通了。

---

## 七个环境变量

踩完这个坑，我把 bot 真正需要的环境变量全列了出来。一共七个。为什么是 7 个不是 5 个？因为每少配一个都会以一种你意想不到的方式炸给你看——而且报错信息往往指向完全无关的方向。少一个都会出幺蛾子：

| 变量 | 为什么需要 |
|------|-----------|
| `PATH` | 找到 `claude`、`python3`、`node` 这些命令 |
| `HOME` | Claude Code 要读 `~/.claude/` 目录下的配置和 OAuth token |
| `http_proxy` | Claude API 在海外，必须走代理 |
| `https_proxy` | 同上，HTTPS 也得走 |
| `NO_PROXY` | 飞书和微信的 API 在国内，不能走代理（否则反而不通） |
| `NODE_PATH` | Claude Code 底层是 Node.js，需要找到全局 npm 模块 |
| `LANG` | 处理中文时的编码设置，不设可能乱码 |

其中 `NO_PROXY` 很容易忘。一开始我只加了代理，结果飞书消息发不出去了——因为飞书 API 在国内，走了代理反而不通。你得告诉程序"这些地址不要走代理"：

```
NO_PROXY=open.feishu.cn,.feishu.cn,.weixin.qq.com,localhost
```

教训就一条：**后台服务不会继承终端的任何东西。你以为"理所当然"存在的配置，在 launchd 里一个都没有。**

---

## 另一个反直觉的事：exit code 不能信

bot 稳定跑起来之后，我发现另一个诡异的现象：Claude Code 明明回复了正确答案，但进程的退出码（exit code）却是 1。

按照 Unix 的传统，exit code 0 = 成功，非 0 = 失败。所以我的代码一开始是这样写的：如果 exit code 不是 0 就报错。结果经常误报。

追了半天发现原因：Claude Code 内部连接的某个 MCP server 超时了（比如一个插件没响应），它就认为"这次执行有错误"，exit 1。但实际上用户的问题它已经正确回答了——回答内容就在 stdout 事件流里，`{"type": "result", "subtype": "success"}` 都打出来了。

所以我最终的判断逻辑是：**不看 exit code，只看 stdout 事件流里有没有 `result.success`**。有就算成功，没有才算失败。

这违反了我十几年写 shell 脚本的直觉（`set -e` 教会你 exit code 是圣旨），但在这个场景里确实是对的。

---

## 加微信：200 行搞定

飞书通了之后，我顺手把微信也接了。用的是一个叫 openclaw 的微信 IM Bot 工具，协议非常简单——三个接口：

- `notifyStart`：告诉服务端"我上线了"
- `getUpdates`：长轮询拉新消息（服务端 hold 住请求，有新消息才返回。跟 Telegram Bot API 一个思路）
- `sendMessage`：发消息

核心逻辑 200 行就通了。关键设计：飞书和微信**共享同一个 Claude 进程和消息队列**。两个通道只是"收消息"和"发回复"的方式不同，中间的处理逻辑完全一样。相当于两扇门通向同一个房间。

有个小坑差点栽进去：微信 API 的域名（`ilinkai.weixin.qq.com`）是国内服务器，直连就行。但因为我全局配了代理，httpx 会让所有请求都走 SOCKS 代理——包括本来就能直连的微信请求。结果报错：`socksio package not installed`。

解法就是前面说的 `NO_PROXY`：把微信域名加进去，告诉程序"这个地址不走代理"。

```python
os.environ["NO_PROXY"] = "open.feishu.cn,.feishu.cn,.weixin.qq.com,localhost"
```

---

飞书、微信两条通道都通了，简单任务秒回。我本该收手了——但你知道程序员是什么人。能自动化一件事，就会想自动化所有事。

如果我在地铁上发一句"帮我把这个两千行的文件拆成八个模块"——它能自己搞定吗？
