# Trae / CPA 自定义上游

本分支增加独立的 `trae` 转发路线。用户提供自己的 Trae / CPA 服务地址和客户端凭据，矿工保留原始模型名、请求正文、用量和流式响应，不把 CPA 标成 OpenAI、Z.ai 或其他官方供应商。

## 配置与使用

运行环境需要同时注入以下变量：

- `TRAE_CPA_BASE_URL`：形如 `https://your-cpa.example/v1`，允许显式 HTTPS 端口。
- `TRAE_CPA_API_KEY`：一个 CPA 客户端凭据，从 Keychain / Secret Manager 注入进程，不写入仓库、镜像或配置示例。
- `GM_NODE_SECRET`：矿工原有的请求认证凭据。
- `GM_NETWORK`：原有 `testnet` / `mainnet` 选择；其含义不变。

地址和凭据都未设置时，新路线完全不启用。只设置一项、缺少矿工认证、使用 HTTP、URL 带用户名密码或查询参数、密钥包含多个槽位或空白字符时，启动失败。错误消息不会回显 URL 或凭据。

请求需要携带 `x-gm-provider: trae` 和原有 `x-gm-node-key`，只接受：

- `GET /v1/models`
- `POST /v1/chat/completions`

请求中的模型名必须是 CPA 真实支持的模型名。本任务使用 Trae 的 DeepSeek，不能把 GLM 响应改名成 DeepSeek，也不自动回退模型。连接 CPA 时验证 HTTPS 证书及主机名；入站供应商凭据、矿工认证头和内部 `x-gm-*` 头不会转发给 CPA。429、Retry-After 和其他上游错误原样返回，绝不自动重试或切换账号。

出站请求统一使用真实的应用标识 `User-Agent: gm-miner-trae/0.5.12`，避免继承 Python 默认客户端标识引发 Cloudflare Browser Integrity Check 的 1010 拒绝。这不是浏览器伪装，不需要关闭 Cloudflare 防护；HTTP 错误仍不重试。

## 构建与部署范围

`image/Dockerfile.trae-cpa` 固定使用官方 v0.5.12 的镜像摘要，只覆盖本次修改的启动脚本和模板，保留原 CLI、RA-TLS 与证明程序，无需重新编译 Rust。自定义镜像版本显示为 `0.5.12-trae-cpa`。

```sh
docker build --platform linux/amd64 -f image/Dockerfile.trae-cpa -t gm-miner-trae:local .
```

本分支的宿主 `gmcli` 现在支持 `trae` 供应商、运行时 CPA 密钥和独立 compose 测量。容器继续使用官方旧 CLI 渲染 Envoy；Trae 是单凭据转发，不走旧 CLI 不认识的供应商槽位。`dstack/trae-cpa.override.yaml` 仍可用于手工部署，但本分支 CLI 已直接生成相同两个额外环境变量，不再叠加 override。默认部署的原有环境变量列表和测量不变。

宿主 CLI 先用 `cargo build -p gmcli --locked` 构建。仅保存不含凭据的地址：

```sh
target/debug/gmcli set-api-keys --trae-cpa-base-url https://your-cpa.example/v1
```

由父进程从现有 Keychain / Secret Manager 一次取出凭据，通过 `TRAE_CPA_API_KEY` 环境变量传入后续命令；本 CLI 不提供密钥命令行参数，也不把它序列化到 `config.json`。Phala 目前使用 `--env` 文件接口，因此仅在提交时建立独占 0700 临时目录和 0600 文件，Phala 子进程结束或普通错误返回时立即删除。强制杀进程、断电不保证清理；不要把临时目录作为长期保管处。普通非 Trae 部署维持原行为。

先离线查看候选镜像的测量与供给声明，无需登录、密钥或资金：

```sh
target/debug/gmcli --network mainnet trae-plan --image-ref 'registry/your-image@sha256:<64位摘要>' --discount-pct 10
```

输出固定标记 `candidate_only: true`，不会声称已准入。Docker 本地 image ID 不等于已发布 registry manifest digest，不能直接拿它注册。部署使用 `deploy --image-ref <真实已发布的摘要引用>`，必须先匹配目标 registry 的 supported compose/OS 测量；不匹配时仍在创建付费 CVM 前失败。`publish-image-version --trae-cpa` 能计算并提交同一测量，但仍需目标 registry 的管理员密钥，并非普通矿工的自助授权。

本分支只允许显式声明：

```sh
target/debug/gmcli declare-product --provider trae --model deepseek-v4.1-flash --discount-pct 10
```

分支客户端要求 registry 的 `upstream-model-echo` 能力和完整 `/miners/products/routes` 中的精确 `trae/deepseek-v4.1-flash → deepseek/deepseek-v4.1-flash` 映射。缺失、旧版目录或映射到其他模型时，拒绝发送供给 POST；不支持 `--upstream-model` 改名。以上是本分支采用的检查条件，不能据此断言官方后端已经支持 Trae 或这些条件足以准入。普通 `declare-products` 会排除 Trae，需逐个明确声明；向导会保留这项待办，不把其他供应商的批量声明当成 Trae 已完成。

**转发测试成功不等于官方网络可接单。** 自定义镜像及独立 `trae` 路线仍须被目标 registry 接受；公开官方 registry 尚未确认支持，不能用别的供应商名绕过它。

## 验证

无需真实账号或付费模型，用同一 `gmcli` 的官方渲染器执行配置测试：

```sh
GMCLI_BIN=/absolute/path/to/gmcli python3 -m unittest discover -s tests -p test_trae_cpa.py -v
```

启用真实 Envoy 容器与本地 HTTPS 模拟 CPA，验证认证、DeepSeek 请求和用量透传、SSE 首块提前到达、未知路由拒绝及 429 不重放：

```sh
GMCLI_BIN=/absolute/path/to/gmcli GM_TRAE_RUNTIME_TEST=1 \
  python3 -m unittest discover -s tests -p test_trae_cpa.py -v
```

容器测试使用 `image/Dockerfile` 固定摘要的 Envoy 镜像和临时自签测试证书，不关闭 TLS 校验，也不访问真实模型供应商。测试结束自动停止自己的容器并删除临时证书和配置。完整原有路由回归仍使用 `scripts/validate-envoy.sh`。

## 官方主网接入条件（2026-10-27 核验）

官方 `GET /products` 有 active 的 `deepseek/deepseek-v4.1-flash` 产品，表示买家可以请求该模型，不代表任意来源都能供给。`GET /image-versions` 当时只列出 v0.5.11、v0.5.12 官方镜像；本分支的自定义镜像不在清单中。`GET /miners/products/routes` 需要矿工登录，匿名请求返回 401，不能把产品列表当成已核实的 Trae 准入结果。

已核实的技术约束是：官方 CLI 部署前后都会比较 compose/OS 测量与 registry 的 supported ImageVersion；不匹配则拒绝注册。发布新条目有 `gmcli publish-image-version` → `POST /admin/image-versions` 的管理员密钥接口，官方发布 CI 也通过此接口自动更新。因此，普通矿工登录不等于拥有发布新镜像条目的权限；这些证据并不证明必须提 GitHub issue、提交人工申请或等待某种固定审批流程。Trae 来源的自助准入或现有兼容路径仍未核实，不能把人工询问当成唯一接入方法。

官方原版 `gmcli` 的 `Provider` 枚举没有 `trae`；本分支虽已补齐 CLI 合同与本地验证，但运行 `--provider trae` 仍不代表官方网关已接受。仍需核验当前账号可见的供给路线、实际 registry 接受的 provider/model 合同和镜像测量；可复用现成路径时优先复用，只有已证实必须新增受控条目且当前身份没有权限时，才记录对应的外部权限缺口。此分支尚未完成主网注册验收。

真实 Trae DeepSeek 小样本已通过本地修改后的矿工转发：普通请求和 SSE 请求均返回 200、答案 `15` 和 `model: deepseek-v4.1-flash`，SSE 有完整 `[DONE]`，CPA trace 均归属 Trae。非流式字段修复部署在配套 CPA 的 Trae 插件 `0.12.75-cpa-transport-cache-prompt-model.4`；矿工转发层仍透传返回值。插件优先保留上游非空 `metadata.model`，上游缺省时使用宿主实际解析并派发的模型名。现有流式字段也来自请求所选模型，因此两种响应的字段都不能作为独立的模型身份认证。原有空字段测试记录保留；本次修复还通过上游返回不同模型名时不覆盖的隔离宿主验证。此验证没有 Phala TDX 证明、官方网关调度或主网结算，不应当成主网上线成功。

## 本分支 CLI 验证

`cargo test -p gmcli --locked` 覆盖凭据不落配置、不在 Debug 回显、地址校验、独立测量、临时密钥文件清理、离线命令，以及模拟 registry 的缺路线、错误买家映射和正确 Trae 声明。模拟 registry 通过只证明客户端行为，不能证明官方注册端已接受。原有官方镜像测量回归保留。
