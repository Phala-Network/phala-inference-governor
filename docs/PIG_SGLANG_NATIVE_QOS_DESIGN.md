# PIG/SGLang：插件化精简执行计划

更新：2026-09-19。用户最新架构指令优先：独立 Rust 控制核心、薄 Python 插件、少量明确可维护的 SGLang 接入点；删除旧实验路径与重复职责，不搬文件掩盖耦合、不用 monkeypatch、不重写 SGLang。

## 产品合同

- 平均 TPS 是软参考，允许上下波动；无逐请求/逐窗口 TPS 硬门槛，不新增排队或 TTFT 硬拒绝。
- 外部鉴权 `GET/PATCH /admin/v1/predictive-policy` 保留 `tps_reference`、epoch/revision CAS；热修改不重启模型、不清空真实历史。
- Rust 核只处理真实 Decode tokens/序列秒数和有界调度建议；不拥有 Req、KV/cache、tensor、ACK 或资源释放权，不把预测 token 写入实测窗口。
- Python 接入复用原生生命周期、控制 IPC；worker 同步、真实分配和取消清理由 SGLang 原生路径拥有。
- TAIL 的信任/TEE 边界保持独立；旧全预测、日志、资格门等实验实现不机械迁入 Rust。原 A–F 中仍有意义的性能、资源与可信链验证继续保留，不能用旧测试数量替代新架构验收。

## 当前实施与验证

- 实际任务目录：`C:/Users/zozyo/Downloads/phala/phala-models-compose/phala-models-compose/tmp/pig-sglang-native-qos-20260915`。自动工作树不是本任务编辑现场。
- 新独立仓库：`C:/Users/zozyo/Downloads/phala/phala-inference-governor`（GitHub: Phala-Network/phala-inference-governor，公开）；精简 SGLang 候选：`source-plugin/`，基于开发基线 `1a56fbb0dc48ec3fb2b4b629fc4e74a3b57c639a`。原 `source/` 和 r44 证据保留供复核/恢复，不作为新插件依赖。
- 插件使用零第三方依赖 Rust cdylib、版本化 C ABI 和 ctypes；服务启动使用预构建库，不现场 Cargo build。
- CPU 集成通过 7 Rust +110 Python 测试；认证修正又通过14项定向测试（12重复、2新增），r5完整候选进一步通过7 Rust +116 Python、零跳过（新增控制请求关联回归）。覆盖真实SGLang方法/msgspec、CAS/鉴权、取消、RID、PrefillAdder、worker metadata、grammar。
- 补丁文本复现通过：r5明确规范化基线CRLF为LF后，25个所选源码/测试文件与候选一致。不是原始字节一致或纯净上游升级证明。
- 同一开发基线、包含注释空行：旧运行 +13061/−272、测试 +19891/−2；当前候选运行 +1074/−104、测试 +1759/−0。含独立安全补丁，19个上游运行文件；插件自身269行Rust+329行Python。
- 开发模型已切换至v0.5.20插件候选，协议与多模态取消验收已完成；1050请求固定负载比较与孤立retraction已完成，吞吐基本持平；可信链及交付仍有未完成项。详见[实际验收记录](validation/DEV_V0520_ACCEPTANCE.md)。初版显式限制TP1/PP1/DP1、non-overlap、无PD；支持范围之外启动拒绝，不伪称已验证。

- v0.5.20独立候选：官方commit 94602c9c2b7cbdb8efd5c52802dac6a1c180089e，本地source-plugin-v0520；Python改读effective namespaces，Rust/C ABI不变。126项Python CPU测试通过、零跳过；三组版本化补丁按序复现25个所选源码/测试文件。该CPU验证使用旧固定镜像依赖；后续已另行安装新版依赖并完成真实模型启动及部分验收，见实际验收记录。见[版本补丁](../patches/sglang/v0.5.20/README.md)。

## 必须保留的接点与补丁理由

| 范围 | 理由 |
| --- | --- |
| scheduler 显式加载、P选择、结果、abort、控制分支 | 让插件只读取/建议并在真实事件时计量；无替换类、动态方法包装 |
| HTTP 管理路由与一处控制消息类型 | 保留外部热修改和CAS；复用既有IPC，明确鉴权及单次在途控制 |
| 通用断连/asyncgen/SHM cleanup | 断连、发送失败或取消时不泄漏生成器、RID、共享内存 |
| worker metadata/grammar 独立补丁 | 保留已有同stream元数据交接与恒等mask修复，不与QoS模块绑定 |

- r5运行包已生成；固定镜像运行源码基线与原生命令解析通过，CLI去除7类旧native-QoS参数，其余模型/上下文/KV/EAGLE配置保持。旧adapter的epoch gate不兼容新插件，既有Go TAIL已去耦并通过23项顶层测试+4项子测试、race/vet/静态构建及依赖闭包检查；实际模型链路已切换至CGO1 TAIL和v0.5.20候选，完成基础协议/Admin/多模态取消验收；详见[模型接入步骤](DEV_MODEL_INTEGRATION.md)。

## 迁移范围（用户最新纠正）

以已修复并验证的Qwen SGLang 0.5.19为参考，先去掉0.5.20已等价覆盖的修复，再移植剩余必要部分并处理冲突。复用原针对性回归，不把尚未移入的旧修复当作新模型故障，不重新设计模型服务。独立工作树只用于保护已验证版本。schema/tool部分已移植并通过89项原协议回归+45项受影响Governor/cleanup测试；reasoning/history/minimum-thinking、媒体/watchdog/pre-MM也已按原合同移植：437项通过、2项CUDA专用测试未在CPU环境执行；只复跑两组验证接入问题，其余12组沿用相同源码结果。进程reap与GDN保留上游已覆盖实现。原部署/发布授权边界不变。

依赖候选已隔离固定kernel0.4.7、deep-gemm0.2.0、nvshmem4py0.3.1、Cython3.3.0，声明约束无缺项；已随候选完成GPU模型启动和协议/多模态验收；固定负载已完成；TLS v2、GPU官方策略及TDX标准密码学验证通过，但strict dynamic_platform失败和启动度量覆盖仍待解决。沿用既定实机验收，不重开模型设计或重复无关测试。

生命周期修复已进入govdev2：只读RID/encoder派发计数和首次排队时间修复，32项定向CPU回归通过；第四组补丁与原三组在官方源码上复现26个改动文件。旧govdev1已停止保留，精确恢复包已下载逐项核验。实机三次内核观测未见MM SHM句柄/映射或命名段；不等价于Python所有权已验收。最新发现成功的默认/字符串RID、n>1请求会遗留未派发父状态；本地修复与真实batch路径回归已准备，尚待red/green执行和最终镜像验收，不在未修复现场主动制造泄漏。

## 下一步验收

2026-09-19发布准备进展：PIG TAIL独立源码提交 `d46290f4e097b4008a3e3bea53323aec9330a32f`
已推送公开仓库并完成远端readback；独立模块CPU测试/race/vet、依赖校验和CGO构建通过，
尚未打版本标签或发布镜像。Governor官方CLI TOKEN接入及受影响回归最终88项方法通过、零跳过，
证据 `validation/governor-token-auth-cpu-r2.json`；先前n2测试发现方式失败保留，补跑仅该两项。
运行时配置/IPC保留真实key，日志/readback使用独立脱敏序列化；正式入口仍是 `sglang serve`。
这不代替最终镜像认证与实机n>1资源排空验证。

1. 核验本轮源码、Rust库、补丁和证据包，形成可交付的可复现插件与上游补丁清单。
2. 对实际模型验证Admin路径/代理兼容、协议/取消/retraction/资源排空；任何替换先履行下方恢复规则。
3. 同模型、到达轨迹和安全配置测平均TPS、完成吞吐、TTFT/队龄、公平、CPU及错误/OOM；0.25s软偏好是新明确策略，不能宣称与旧算法等价或已有收益。
4. 复核更广拓扑和上游升级接点；必要支持必须有同步/资源证据，不用放宽guard替代。
5. 核对剩余可信链与发布要求，报告最终代码/测试数量、Rust采用和运行边界；未完成时goal保持active。

## 当前现场与授权边界

- 开发CVM `805bfe7c-00f5-4e14-995a-8f25631b7703` 本任务Qwen/TAIL。2026-09-19 fresh status确认backend `e7ae81fd…c6d48d`，版本0.5.20+phala.govdev2，StartedAt `2026-09-19T13:29:05.481416279Z`，epoch `6652b3ceb6ad445aa1a36a2adeeeea23`，35/revision1，无重启/OOM，六项owner计数均零；govdev1和r44已停止保留。证据：`governor-v0520-status-release-prep-r1.json`，SHA256 `efd910b81079156753b4fcd58c1a51a27d3447da3ced8d374f7affd2c8db621e`。Running/空闲状态不是新实机协议验收。
- 当前TAIL `1a9bae9f…0fda`；私网 `pig-native-qos-private`，仅TAIL绑定127.0.0.1:31080。r44旧adapter及r43停止保留。
- 每次替换前保存当时serving精确恢复、下载逐项核验、三次fresh drain、保留旧容器。七运行非目标与停止Muse/Nemotron保持。
- 不读取/扫描/备份/hash/修改prelaunch；不执行无关CVM重启、Router/Redpill路由或其他生产目标更改。
- 用户已授权：本轮检查基本可用后，Governor和独立PIG TAIL提交/推送首版v0.1.0，按生产标准打包并发布TAIL和带Governor的SGLang镜像，部署到唯一生产测试目标 `e4bd4036-c788-45fd-bbac-3c649ef522b8`。这一授权替代历史同范围commit/push/镜像发布/正式版本/生产测试禁令，不需重复审批；不扩展到其他CVM或路由修改。
- Governor仓库为公开 `Phala-Network/phala-inference-governor`；TAIL独立仓库为公开 `Phala-Network/pig-tail`，保持用户设置的可见性。最终镜像必须从干净源码提交和锁定官方基线构建，包含必要的Qwen兼容修复，不提升开发镜像或使用可执行挂载/隐藏launcher。已知strict dynamic_platform失败与启动度量证据缺口如实保留，不宣称完整可信验收。
- Windows仅编辑/Git/归档/hash/证据读取；全部编译、测试、AST和分析在805固定c4fe镜像、runc、GPUvoid、1CPU。性能客户端无CPU quota。
- 根.env统一TOKEN仅stdin；Invoke-Builder.ps1 -TokenInput，绝不-TestHost。模型/revision、262144 context、FP8 KV、EAGLE、CC不为收益而改变。
- 子任务按实际难度、风险、上下文与验证成本选模型/reasoning；独立上下文，主代理整合和验收。

详细入口：[插件README](../README.md)、[补丁说明](../patches/sglang/README.md)、[验证清单](validation/governor-plugin-source-audit-r5.json)。r44与旧实验资料继续在原任务目录保存；后续Governor代码只在独立仓库开发。
