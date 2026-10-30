# 跨服务器 HTTPS 安装（v0.19）

v0.22 将项目自身 HTTP/HTTPS 监听等待队列显式设为 64，不修改宿主 TCP 配置，TLS 仍最多 64 个在途连接、握手 5 秒/请求 8 秒超时。真实编译期间两轮各 20 个逻辑账号的本机 HTTP 读取全部成功、越权任务读取均 404；该有限样本的 p95 从约 1.03 秒降至 0.59 秒，中位数从 0.14 秒升至 0.30 秒。分接口及限制见 evidence/read-load-comparison-validation.json；不是 TLS、多物理机、NPU 混合负载或一般吞吐提升的证明。中央重载后确认同编译容器/attempt/StartedAt及预留不变；第一次重载后即时状态未记录，不声称全程无状态过渡。

中央服务可以直接监听 HTTPS，节点、个人 CLI 和 MCP 都使用同一个经过证书校验的 API。默认仍只监听 `127.0.0.1` 的 HTTP，供本机或 SSH 隧道使用；非 loopback HTTP 会在预检和启动时拒绝。当前原生监听支持 IPv4 字面地址，不自动改防火墙、DNS、路由或系统信任库。

## 中央安装

管理员准备与访问域名匹配的证书链、私钥和可选私有 CA PEM。源文件必须是受保护目录下的普通文件；不直接跟随证书管理系统的符号链接。私钥须600，Root安装时源文件及祖先目录由Root保护。安装器校验证书/私钥配对、主机名、信任链和有效期，最低TLS1.2；验证用内存握手，不监听端口、不联网、不创建文件。私有CA须显式提供，公有证书可使用系统信任链。

```bash
./servicectl doctor --role controller --archive /root/provision/ascend-lab-server.tar.gz \
  --prefix /opt/ascend-lab-controller --state-dir /var/lib/ascend-lab-controller \
  --create-service-user ascendlab --port 18765 --bind-address 0.0.0.0 \
  --public-url https://lab.example.org:18765 \
  --tls-cert /root/provision/server-chain.pem --tls-key /root/provision/server.key \
  --tls-ca /root/provision/company-ca.pem
```

确认报告后，将同一命令的 `doctor` 改为 `install`，可加 `--start` 启动。安装器重新核对包及TLS材料指纹，复制证书和私钥到中央私有状态目录，600权限、专用服务UID拥有；无特权中央不需要读取Root原始私钥。Node、个人客户端、源码包和发布包均不包含服务端私钥。加密私钥不在无人值守启动时询问密码，应使用外部TLS终止服务或管理员准备受保护的非交互密钥。

域名/SAN须覆盖实际访问地址。预检不证明DNS已指向正确主机或外部网络可达，部署后仍需从目标节点实际验证。公网证书省略 `--tls-ca`；私有CA只进入本应用信任文件，不安装到任何全局证书库。无需为了原生HTTPS把中央加入Docker组。

## 节点和个人配置交付

在目标服务器运行 `workerctl identity` 获取物理ID，按SERVER.md在中央登记资源域、用户映射及不可变镜像，再登记节点。中央配置中的HTTPS地址和公开CA会默认用于新登记；也可明确指定 `--controller-url` 和 `--ca-file`。覆盖地址时，管理员还需选择对应的正确信任CA，不能跳过校验。

登记后的私有目录包含 `config.json` 和可选 `controller-ca.pem`。通过私有通道将**整个目录**交付给目标节点；不要只复制JSON，不要公开其中的node_key。CA路径在交付配置中相对JSON所在目录解析，与启动时的工作目录无关。Node安装前会解析、校验并冻结CA指纹，再将公开CA复制进本节点私有状态；后续不依赖中央文件路径或共享 `/home`。

```bash
./servicectl doctor --role node --archive /root/provision/ascend-lab-server.tar.gz \
  --prefix /opt/ascend-lab-node --state-dir /var/lib/ascend-lab-node \
  --node-config /root/provision/node-bundle/config.json
```

个人 `accessctl user-add` 生成的目录同样包含个人 `client.json` 和可选 `controller-ca.pem`。交付给对应用户，保留目录700、凭据600；公开CA不含私钥。独立客户端0.19支持相对CA路径，CLI、MCP和 `Client.from_profile` 都能迁移整个配置目录；旧绝对CA路径仍可用。客户端仍拒绝非loopback HTTP、跳过证书校验和自动重定向。

## 同一 CA 下的证书续期

例行续证保持公开地址及CA bundle不变。原生运行配置固定了材料指纹，不能只覆盖私钥文件就指望新进程接受变化。按以下流程更新：

```bash
./servicectl stop --prefix /opt/ascend-lab-controller
./servicectl tls-rotate --prefix /opt/ascend-lab-controller \
  --tls-cert /root/provision/renewed-chain.pem --tls-key /root/provision/renewed.key \
  --tls-ca /root/provision/company-ca.pem
./servicectl start --prefix /opt/ascend-lab-controller
```

公有证书、原先未配私有CA时省略 `--tls-ca`。命令要求Root及停止的中央服务；先验证新证书，写入新私有目录后原子更新配置，保留旧材料，不改账户、token、节点密钥、任务状态或公开地址。同一材料重复调用不生成新目录。既有客户端和节点继续使用原CA，中央重启后的执行清单保护仍然生效。

CA bundle内容改变会被例行续期命令拒绝，需要另行设计新旧信任并存的过渡，不自动替换所有人的信任。证书材料仍未写入个人Agent配置。备份包含私有TLS材料，必须按SERVER.md放在Root私有目录；恢复到新状态目录会同步迁移TLS和CA路径，原地址继续保留。证书更新不是全自动证书签发或续期服务，没有创建定时任务。

## 当前验证边界

Root演练将中央与节点放入两个不同的网络命名空间，使用自有内部Docker网桥和独立CA，通过DNS地址进行节点认证、环境探测、实际IR、归档和备份恢复；宿主测试客户端直接连接自有网桥地址，未发布宿主端口。任务容器仍为无网络、非Root、只读根，无Docker socket或服务私钥。

本轮还验证未信任证书拒绝、同源HTTPS写入、错误scheme的Origin拒绝、慢TLS连接、相对CA迁移和同CA续证。它证明原生HTTPS和独立网络边界可运行，不是两台物理服务器、真实外部网络、防火墙策略、CA轮换、系统级证书签发、systemd/PID1或NPU运行时验收。公开入口仍需管理员决定可访问网络及账户授权。
