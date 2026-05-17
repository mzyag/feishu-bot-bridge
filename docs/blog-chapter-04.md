# AI 执行力太强是一种灾难

系统上线第二天，我在地铁上随手发了句"帮我加个用户认证"。十五分钟后出站看手机，项目代码面目全非。

这是我第一次体会到：**AI 不会说"你确定吗？"**

---

## JWT 翻车现场

那天我在地铁上，随手发了句："帮我加个用户认证。"

我的意思是：给我另一个项目的 API 接口加个简单的 token 校验。一个中间件，十几行代码的事。

但 AI 的理解是：给飞书 bot 本身加一套完整的 JWT 认证系统。

它规划了五步计划：
1. 设计用户权限模型
2. 实现 JWT 签发/验证/刷新
3. 给所有接口加认证中间件
4. 添加异常处理和日志
5. 更新文档

然后它就开始执行了。DeepSeek 出了完整方案，Claude Code 拿着方案认认真真地改起了代码——新建了 `auth/` 目录，加了 `pyjwt` 依赖，给 `ws_bot.py` 裹了三层装饰器……

等我出站看手机，项目已经面目全非。代码质量不错——如果我真的需要一套 JWT 系统的话。但我不需要。方向完全错了。

我面无表情地敲了：

```bash
git checkout .
```

所有改动消失。十分钟的执行结果归零。

这件事让我意识到一个问题：**AI 不会说"你确定吗？"** 它执行力太强了。你随口一句话，它就能给你搞出一个完整的工程方案并且立刻动手。如果方向是对的，这是超能力；如果方向是错的，这是灾难。

---

## 两个人工卡点

翻车之后我加了两个硬卡点，强制让人确认才能继续：

**卡点 1：需求确认**（`awaiting_confirmation`）

AI 分析完需求后，先告诉我它打算做什么。我说"确认"才往下走。

**卡点 2：计划确认**（`awaiting_plan_approval`）

执行计划列出来后，我看一遍。确认合理才开始执行。

技术上就是在 workflow 状态机里加了两个 phase：

```python
workflow["phase"] = "awaiting_confirmation"  # 等用户确认需求理解
workflow["phase"] = "awaiting_plan_approval"  # 等用户确认执行计划
```

只有收到用户的确认回复，phase 才会推进到 `executing`。AI 没拿到批准之前，一行代码都不会动。

---

## "好"字误判事故

加了卡点之后我以为安全了。直到有一天——

我回了句"不好意思我在忙"。

bot 从这句话里看到了一个"好"字，判定为确认，直接开始执行。等我反应过来的时候代码已经改了三个文件。

原来我的确认逻辑写的是：

```python
if "好" in user_text:
    approve()
```

太粗暴了。"你好"、"不好"、"好像不对"——全会命中。

改成精确匹配：

```python
if user_text.strip() in ("确认", "确认执行", "可以"):
    approve()
```

从此再没出过事。教训：**跟 LLM 打交道，用户意图的判断绝对不能用模糊匹配。**

---

## Watchdog：进程假死怎么办

人工卡点解决了"方向错"的问题。但还有另一个问题：**进程假死。**

Claude Code 的持久进程有时候会进入一种状态——`ps aux` 能看到它，`proc.poll()` 返回 None（说明它"活着"），但 stdout 不再输出任何内容了。卡住了。

我遇过好几次：团队模式跑到第三步，突然没动静了。等五分钟、十分钟，还是没有。手动 kill 重启就好了。

解法是加了个 Watchdog daemon 线程：

```python
def _watchdog_loop(self):
    while self._alive:
        time.sleep(10)
        idle = time.time() - self._last_stdout_ts
        if idle > 90:  # 90秒无输出，判定僵死
            self._proc.kill()
            self._alive = False
```

核心逻辑：
- 记录每次 stdout 有输出时的时间戳 `last_stdout_ts`
- 每 10 秒检查一次：如果超过 90 秒没有任何 stdout 输出，判定为僵死（为什么是 90 秒而不是 60 秒？因为 Claude 处理复杂任务时正常的思考间隔可以到 40-50 秒，60 秒会误杀正常请求。90 秒留了足够的安全余量）
- 直接 kill 掉进程

下次 `send_message` 发现进程死了，会自动重启。但重启不能太暴力——万一是 Claude 那边暂时维护呢？连续快速重启只会打爆对方。

所以加了 exponential backoff——就像敲门没人应，你不会一直狂敲，而是等一秒再敲、等两秒再敲、等四秒再敲，给对方喘口气的时间：

```python
self._restart_backoff = min(self._restart_backoff * 2, 60)
time.sleep(self._restart_backoff)  # 1s → 2s → 4s → 8s → 16s → ...
```

重启成功后 backoff 归零。

上线之后有一天早上看日志，发现凌晨 3 点 watchdog 连续重启了 4 次——间隔 1 秒、2 秒、4 秒、8 秒。估计是 Claude 那边在做维护。但因为有 backoff，没把对方打爆，等恢复了自动就好了。如果没有 watchdog，第二天早上我就得手动介入。

---

## 状态持久化

还有个容易忽略的问题：用户发了需求，AI 回了"请确认"，用户五分钟后才回"确认"。但这五分钟里 bot 可能因为异常重启了——内存里的 workflow 状态全没了，它不知道之前在等什么确认。

解法很直接：workflow 状态写到 JSON 文件里。

```python
def _save_workflow():
    path = Path(".state/workflows.json")
    path.write_text(json.dumps(_WORKFLOW_BY_USER, ensure_ascii=False, indent=2))
```

每次 phase 变化都写一次。重启后读文件恢复状态，用户的确认回复不会落空。

---

## LLM 输出格式不可信

最后一个教训：**永远不要假设 LLM 的输出是你期望的格式。**

我在 prompt 里写了"只输出 JSON，不要其他任何文字"。加粗了。大写了。Claude 大部分时候确实只输出 JSON。但时不时它会加一句：

```
好的，以下是 JSON 输出：

```json
{"action": "execute", "plan": [...]}
```​
```

或者前面加一段"让我分析一下……"然后才给 JSON。

如果你的代码是 `json.loads(response)`，那就直接炸了。

解法：用正则从整段文本里提取 JSON 对象：

```python
match = re.search(r'\{[\s\S]*\}', response)
if match:
    data = json.loads(match.group())
```

这个坑我踩了至少三次才彻底学乖。现在凡是需要 LLM 输出结构化数据的地方，一律正则提取，不假设格式。

---

翻车、误判、僵死、格式不可信——每一个坑都让我在深夜对着屏幕骂过自己。但踩完这些之后，这个系统终于变成了我每天真正在用的东西。不是"能用"，是"离不开"。
