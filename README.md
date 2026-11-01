# 🇭🇰 香港六合彩 (Mark Six) 个人即时看板与真随机选号工具

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/FastAPI-0.110%2B-009688?style=flat-square&logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/Random-secrets.SystemRandom-gold?style=flat-square" alt="True Random">
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License">
</p>

香港六合彩个人即时看板与真随机选号工具，集成了马会实时预估头奖金额、期数、开奖时间同步，内置 Python 物理级真随机无放回抽样发生器，支持官方红/蓝/绿波色 3D 拟真球号渲染、单注/全部一键复制、账号密码安全认证以及一键自动化部署与多端口/复用证书配置。

---

## ✨ 核心特性

- **🎲 物理级真随机抽样**：严格采用 Python `secrets.SystemRandom()` 模块调用操作系统内核 CSPRNG 熵源（`/dev/urandom` / `CryptGenRandom`），从 1~49 中无放回抽取 6 个不重复号码并自动升序排列，彻底杜绝伪随机。
- **📊 马会官方即时看板**：直连香港马会官方实时数据源，动态抓取下期预估头奖基金（奖池金额）、最新期数、开奖日期及截止投注时间，并提供上期中奖回顾。
- **⚡ 高并发与智能缓存**：内置 1 小时线程安全内存时间戳缓存（Thread-Safe TTL），抵御马会风控限制；网络异常时启用平滑保底容灾。
- **🔴 官方红/蓝/绿波色 3D 渲染**：依据香港马会官方 49 码规则精准映射波色，CSS 3D 球体高光与阴影质感渲染，自动格式化双位数（`01`, `02`...）。
- **🔒 账号密码访问控制**：支持环境变量与 `.env` 配置管理员账号密码，前端提供现代毛玻璃拟态登录弹窗与退出功能，保障个人工具私密性。
- **🌐 灵活端口与证书复用**：部署脚本自动检测 443 端口占用；当服务器已运行 `cdn-cf-optimizer` 等服务时，支持**自定义 HTTPS 端口**（如 8443）并**直接复用已有 SSL 证书**，或直接在现有 443 反代中通过子路径并存。

---

## 🚀 极速一键部署

在您的 Linux 云服务器上执行：

```bash
git clone https://github.com/yueyingzxq/lucky_marksix.git && cd lucky_marksix && sudo bash deploy.sh
```

### 💡 443 端口已被占用（如已部署 cdn-cf-optimizer）时的两种方案：

#### 方案 1：使用独立端口 (如 8443) 并复用现成证书 (脚本已全自动支持)
运行 `sudo bash deploy.sh` 时：
1. 脚本检测到 443 已被占用，会提示您指定新的 HTTPS 端口（例如 `8443` 或 `9443`）；
2. 证书获取方式选择 `[1] 复用已有证书`，输入 cdn-cf-optimizer 现有的 `fullchain.pem` 和 `privkey.pem` 路径即可；
3. 部署后访问地址形如：`https://yourdomain.com:8443`。

#### 方案 2：免改端口！直接在现有 443 服务中追加反代
如果您希望不带端口号，继续在 443 端口访问 `https://yourdomain.com/marksix/`：
- **若是 Nginx**：在您现有的 443 站点配置（如 `/etc/nginx/conf.d/cfsub.conf`）中追加：
  ```nginx
  location /marksix/ {
      proxy_pass http://127.0.0.1:8000/;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
  }
  ```
- **若是 Caddy**（如 `cdn-cf-optimizer` Docker 容器的 Caddyfile）：在域名大括号内追加：
  ```caddy
  handle /marksix/* {
      reverse_proxy 127.0.0.1:8000
  }
  ```
- 执行 `systemctl reload nginx` 或 `docker compose restart caddy` 即可直接生效，证书完全免配！

---

## 🛠️ 运维与管理命令

```bash
sudo bash deploy.sh status    # 查看当前服务运行状态
sudo bash deploy.sh restart   # 重启应用服务与 Nginx
sudo bash deploy.sh update    # 从 GitHub 拉取最新代码并重启
sudo bash deploy.sh stop      # 停止服务
sudo journalctl -u marksix -f # 实时查看系统运行日志
```

---

## ⚙️ 环境变量说明 (`.env`)

| 变量名 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `HOST` | 监听地址 | `127.0.0.1` |
| `PORT` | 本地 FastAPI 后端端口 | `8000` (冲突可自动改 8001) |
| `AUTH_ENABLED` | 是否开启账号密码登录模式 | `true` (设为 `false` 则为公开免密) |
| `AUTH_USERNAME` | 管理员用户名 | `admin` |
| `AUTH_PASSWORD` | 管理员密码 | 自定义设置 |
| `SECRET_KEY` | 签名与会话安全密钥 | 随机生成字符串 |
| `CACHE_TTL_SECONDS` | 马会奖池抓取缓存时间 (秒) | `3600` (1 小时) |

---

## 📄 开源许可

本项目基于 MIT 协议开源，仅供个人技术研究、数据统计与娱乐参考使用。
