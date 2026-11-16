# Muse 部署交接

## 交付目标

将本仓库部署到用户的云服务器，提供 iPhone Safari 可访问的 HTTPS 地址。Python 服务持续监测学习通课程中的手势签到。网页支持登录、启动与停止、修改候选手势和查看日志。

默认候选依次为 `1235789`、`14789`，轮询间隔 30 秒，候选间隔 3 秒。当前支持课程手势签到。尚未验证真实学习通登录和课堂签到，部署成功后应由用户登录本人账号验证。

## 部署步骤

1. 取得用户提供的服务器连接方式和域名。先检查操作系统、可用内存、Docker/Compose、已有反向代理，以及 80/443 端口占用。
2. 克隆仓库至服务器的独立目录。保留既有服务和站点配置。仓库公开，克隆无需 GitHub 凭据。
3. 若 80/443 可用于本项目，采用仓库的 Compose+Caddy 方案。创建 `.env`，设置 `SIGN_DOMAIN` 为实际域名，并确认 DNS 指向服务器。若主机已有 HTTPS 反向代理，可复用它，将应用映射到本机空闲端口，并把该站点代理到应用。Python 的公开入口由 HTTPS 代理提供。
4. 执行 `docker compose config --quiet`，再执行 `docker compose up -d --build`。
5. 检查容器状态、证书和页面。`GET /` 应返回 200，`GET /api/status` 在未提供密钥时应返回 401。
6. 访问密钥首次启动生成在 `data/work/panel-key.txt`，通过安全终端交给用户。避免将密钥内容写入日志、提交到 GitHub 或放入 URL。用户打开网站后先输入访问密钥，再登录学习通。
7. 用户登录后点击「开始监测」。确认运行记录和最近检测时间更新。服务器重启会清空内存会话，需要重新登录并开始监测。

## 部署结构

- `src/web.py` 提供网页/API，Waitress 使用 4 个线程，后台任务独立于浏览器运行。
- `src/main.py` 提供协议适配、候选处理、去重状态和 Windows 命令行入口。
- `src/mobile/` 包含手机页面，无 Node 构建步骤。
- `Dockerfile` 基于 Python 3.12，容器监听 8080，仅在 Compose 内部网络提供服务。
- `compose.yaml` 使用 `./data:/data` 保存配置、去重状态、日志和面板密钥，Caddy 使用独立命名卷保存证书。
- `Caddyfile` 从 `SIGN_DOMAIN` 读取站点域名。

应用要求远程登录走 HTTPS。只有位于一层可信反向代理后的应用才使用 `--behind-proxy`，Compose 默认符合这个结构。

## 验证命令

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=50
```

GitHub Actions 在每次推送时执行离线测试、镜像构建和容器 HTTP 冒烟检查，可查看 Actions 中对应提交的结果。

建议为应用与 HTTPS 代理预留约 256MB 内存，实际以服务器测量为准。此前 Windows 本机网页服务实测约 50MB，未登录学习通，未计入 Linux、Docker 与 Caddy。

## 更新和排错

更新前备份 `data/` 和 Caddy 卷，保留账号对应的去重记录。拉取代码后重新构建启动。启动失败先检查日志、目录权限、域名解析和端口冲突。

明确拒绝过的手势不会对同一活动反复提交。所有候选不匹配时可在网页新增已知手势。未知响应或额外验证会暂停该活动，需在学习通中核对状态。处理完后，停止后台任务，再编辑 `data/work/state.json` 中对应活动记录。避免清空其他活动的状态。

最终向用户提供实际 HTTPS 地址、容器状态、自动启动配置情况、数据目录、密钥获取方式、真实账号验证结果及剩余限制。
