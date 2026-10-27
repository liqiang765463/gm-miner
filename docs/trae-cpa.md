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

`dstack/trae-cpa.override.yaml` 只提供自定义部署需要的额外环境变量，需与已渲染的基础 compose 合并。原版 `gmcli deploy`、供应商枚举、钱包、镜像测量和路由准入逻辑均保持原样，不会自动将本分支发布或注册到官方网络。

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

接入顺序是：官方维护者确认接受 Trae/CPA 来源 → registry 添加明确的 Trae 供给路线、模型映射与身份检查 → CLI 增加相应供应商/凭据/声明支持，并批准自定义镜像及新增环境变量的测量 → 在测试网验证 → 注册主网 28、部署已获准的 Phala CVM → 声明报价并核验请求、评分及实际结算。

当前改动完成的是独立数据面转发；原版 `gmcli` 的 `Provider` 枚举没有 `trae`，不能直接运行 `--provider trae` 宣布已上线。`POST /admin/image-versions` 需要 registry 管理员权限，普通矿工登录或付注册费不能自行批准新镜像。新增供给路线是否被接受，需要官方维护者确认；此分支没有代替该确认。

真实 Trae DeepSeek 小样本已通过本地修改后的矿工转发：普通请求和 SSE 请求均返回 200，SSE 有完整 `[DONE]` 与正确模型名。此次普通响应的 `model` 字段为空，原样保留；若目标网络要求逐响应验证模型身份，仍需完善非流式兼容，不能凭请求中的模型名伪造身份。此验证没有 Phala TDX 证明、官方网关调度或主网结算，不应当成主网上线成功。
