# 🇭🇰 香港六合彩 (Mark Six) 个人即时看板与真随机选号工具

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/FastAPI-0.110%2B-009688?style=flat-square&logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/Random-secrets.SystemRandom-gold?style=flat-square" alt="True Random">
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License">
</p>

香港六合彩个人即时看板与真随机选号工具，集成了马会实时预估头奖金额、期数、开奖时间同步，内置 Python 物理级真随机无放回抽样发生器，支持官方红/蓝/绿波色 3D 拟真球号渲染、单注/全部一键复制、账号密码安全认证以及一键自动化部署与 HTTPS 证书配置。

---

## ✨ 核心特性

- **🎲 物理级真随机抽样**：严格采用 Python `secrets.SystemRandom()` 模块调用操作系统内核 CSPRNG 熵源（`/dev/urandom` / `CryptGenRandom`），从 1~49 中无放回抽取 6 个不重复号码并自动升序排列，彻底杜绝伪随机。
- **📊 马会官方即时看板**：直连香港马会官方实时数据源，动态抓取下期预估头奖基金（奖池金额）、最新期数、开奖日期及截止投注时间，并提供上期中奖回顾。
- **⚡ 高并发与智能缓存**：内置 1 小时线程安全内存时间戳缓存（Thread-Safe TTL），抵御马会风控限制；网络异常时启用平滑保底容灾。
- **🔴 官方红/蓝/绿波色 3D 渲染**：依据香港马会官方 49 码规则精准映射波色，CSS 3D 球体高光与阴影质感渲染，自动格式化双位数（`01`, `02`...）。
- **🔒 账号密码访问控制**：支持环境变量与 `.env` 配置管理员账号密码，前端提供现代毛玻璃拟态登录弹窗与退出功能，保障个人工具私密性。
- **🌐 生产级自动化部署**：提供完整一键部署脚本 `deploy.sh`，自动配置 Python 虚拟环境、Systemd 后台服务、Nginx 反向代理以及 Let's Encrypt / Certbot HTTPS (SSL) 免费证书。

---

## 🚀 极速一键部署 (推荐)

在您的 Linux 云服务器（Ubuntu 20.04+、Debian 11+、CentOS 8+、Rocky 等）上执行以下一行命令即可：

```bash
git clone https://github.com/yueyingzxq/lucky_marksix.git && cd lucky_marksix && sudo bash deploy.sh
```

脚本将全自动引导您：
1. 安装系统依赖（Python3、Nginx、Certbot 等）；
2. 设置后台管理员用户名与密码；
3. 构建虚拟环境并启动后台服务；
4. 绑定您的域名并自动申请免费 HTTPS SSL 证书（若无域名可选择纯 IP 模式）。

---

## 🛠️ 运维与管理命令

部署完成后，您可以在项目目录中直接运行：

```bash
sudo bash deploy.sh status    # 查看当前服务运行状态
sudo bash deploy.sh restart   # 重启服务与 Nginx
sudo bash deploy.sh update    # 从 GitHub 拉取最新代码并重启
sudo bash deploy.sh stop      # 停止服务
```

查看系统实时日志：
```bash
sudo journalctl -u marksix -f
```

---

## 🐳 Docker 方式部署 (可选)

若您更倾向于使用 Docker / Docker Compose：

```bash
# 1. 复制配置文件
cp .env.example .env
nano .env  # 按需修改 AUTH_USERNAME 与 AUTH_PASSWORD

# 2. 一键启动
docker compose up -d --build
```
服务将在容器内启动并监听宿主机 `8000` 端口。

---

## ⚙️ 环境变量说明 (`.env`)

| 变量名 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `HOST` | 监听地址 | `0.0.0.0` (本地反代可设为 `127.0.0.1`) |
| `PORT` | 监听端口 | `8000` |
| `AUTH_ENABLED` | 是否开启账号密码登录模式 | `true` (设为 `false` 则为公开免密) |
| `AUTH_USERNAME` | 管理员用户名 | `admin` |
| `AUTH_PASSWORD` | 管理员密码 | `marksix888` |
| `SECRET_KEY` | 签名与会话安全密钥 | 随机生成字符串 |
| `CACHE_TTL_SECONDS` | 马会奖池抓取缓存时间 (秒) | `3600` (1 小时) |

---

## 📡 API 接口参考

| 方法 | 路径 | 说明 | 鉴权要求 |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/login` | 管理员账号密码登录 | 无 |
| `GET` | `/api/auth/status` | 查询鉴权状态与登录用户 | 无 |
| `POST` | `/api/logout` | 退出登录并销毁 Token | Bearer Token |
| `GET` | `/api/info` | 获取最新预估头奖、期数与开奖日期 | 需登录 (`refresh=true` 穿透缓存) |
| `GET` | `/api/generate` | 真随机抽取号码 (`count=1~10`) | 需登录 |
| `GET` | `/` | 前端单页面入口 | 无 |

### 选号生成返回示例 (`GET /api/generate?count=1`)

```json
{
  "code": 200,
  "status": "success",
  "count": 1,
  "entropy_source": "secrets.SystemRandom() (Hardware/OS Entropy)",
  "bets": [
    {
      "bet_id": 1,
      "numbers": [6, 12, 19, 25, 33, 47],
      "formatted_string": "06 12 19 25 33 47",
      "balls": [
        {"number": 6, "text": "06", "color": "green"},
        {"number": 12, "text": "12", "color": "red"},
        {"number": 19, "text": "19", "color": "red"},
        {"number": 25, "text": "25", "color": "blue"},
        {"number": 33, "text": "33", "color": "green"},
        {"number": 47, "text": "47", "color": "blue"}
      ]
    }
  ],
  "generated_at": "2026-11-01 16:30:00"
}
```

---

## 🎨 六合彩波色规则表

- 🔴 **红波 (17 码)**：`01, 02, 07, 08, 12, 13, 18, 19, 23, 24, 29, 30, 34, 35, 40, 45, 46`
- 🔵 **蓝波 (16 码)**：`03, 04, 09, 10, 14, 15, 20, 25, 26, 31, 36, 37, 41, 42, 47, 48`
- 🟢 **绿波 (16 码)**：`05, 06, 11, 16, 17, 21, 22, 27, 28, 32, 33, 38, 39, 43, 44, 49`

---

## 📄 开源许可

本项目基于 MIT 协议开源，仅供个人技术研究、数据统计与娱乐参考使用。
