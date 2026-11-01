#!/usr/bin/env bash
# ==============================================================================
# 香港六合彩 (Mark Six) 即时看板与真随机选号工具 - 服务器一键部署与运维脚本
# 支持: Docker 容器化部署 (推荐) & 传统 Systemd 本机部署
# 支持: 自定义端口 (如 8443) 与复用已有 SSL 证书 (如 cdn-cf-optimizer 证书)
# ==============================================================================

set -e

# 颜色输出定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="marksix"
DEFAULT_FASTAPI_PORT=8000

echo -e "${CYAN}${BOLD}"
echo "=========================================================="
echo "   🎲 香港六合彩 (Mark Six) 个人即时看板与选号工具部署"
echo "=========================================================="
echo -e "${NC}"

check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}[ERROR] 请使用 root 权限或 sudo 运行此脚本！${NC}"
        echo -e "示例: ${YELLOW}sudo bash deploy.sh${NC}"
        exit 1
    fi
}

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
    else
        echo -e "${RED}[ERROR] 无法识别当前操作系统！${NC}"
        exit 1
    fi
}

is_port_in_use() {
    local port=$1
    if command -v ss >/dev/null 2>&1; then
        ss -tuln | grep -q ":${port} " && return 0
    elif command -v netstat >/dev/null 2>&1; then
        netstat -tuln | grep -q ":${port} " && return 0
    elif command -v lsof >/dev/null 2>&1; then
        lsof -i ":${port}" >/dev/null 2>&1 && return 0
    fi
    return 1
}

# 停止可能存在的旧 Systemd 服务 (避免与 Docker 争抢端口)
stop_old_systemd() {
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo -e "${YELLOW}[!] 检测到旧的 Systemd ${SERVICE_NAME} 服务正在运行，正在停止并禁用...${NC}"
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true
        echo -e "${GREEN}[✓] 旧服务已释放端口。${NC}"
    fi
}

# 配置应用环境变量 (.env)
setup_env() {
    echo -e "${BLUE}[*] 配置应用环境变量与登录凭证...${NC}"
    cd "$APP_DIR"
    
    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            cp .env.example .env
        else
            cat <<EOF > .env
HOST=0.0.0.0
PORT=8000
AUTH_ENABLED=true
AUTH_USERNAME=admin
AUTH_PASSWORD=marksix888
SECRET_KEY=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24)
CACHE_TTL_SECONDS=3600
EOF
        fi

        echo -e "${YELLOW}请设置后台访问登录凭证：${NC}"
        read -r -p "请输入管理员用户名 (默认: admin): " INPUT_USER
        INPUT_USER=${INPUT_USER:-admin}

        DEF_PASS=$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 10)
        read -r -p "请输入管理员密码 (默认自动生成: $DEF_PASS): " INPUT_PASS
        INPUT_PASS=${INPUT_PASS:-$DEF_PASS}

        sed -i "s/^AUTH_USERNAME=.*/AUTH_USERNAME=$INPUT_USER/" .env || true
        sed -i "s/^AUTH_PASSWORD=.*/AUTH_PASSWORD=$INPUT_PASS/" .env || true
        
        RAND_KEY=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)
        sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$RAND_KEY/" .env || true

        echo -e "${GREEN}[✓] 登录凭证配置完毕！用户名: ${BOLD}$INPUT_USER${NC}${GREEN}，密码: ${BOLD}$INPUT_PASS${NC}"
    else
        echo -e "${GREEN}[✓] 已存在 .env 配置文件，直接读取现有配置。${NC}"
    fi
}

# ------------------------------------------------------------------------------
# 路线 A: Docker 容器化部署 (用户首选)
# ------------------------------------------------------------------------------
deploy_docker() {
    check_root
    echo -e "${BLUE}==========================================================${NC}"
    echo -e "${BLUE}        🚀 启动 Docker 容器化部署流程                     ${NC}"
    echo -e "${BLUE}==========================================================${NC}"

    # 1. 自动停止旧 Systemd 进程
    stop_old_systemd

    # 2. 检查并安装 Docker
    if ! command -v docker >/dev/null 2>&1; then
        echo -e "${YELLOW}[!] 未检测到 Docker，正在自动安装 Docker 环境...${NC}"
        curl -fsSL https://get.docker.com | bash
        systemctl enable --now docker
    fi

    # 3. 检查 Docker Compose
    DOCKER_COMPOSE_CMD=""
    if docker compose version >/dev/null 2>&1; then
        DOCKER_COMPOSE_CMD="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        DOCKER_COMPOSE_CMD="docker-compose"
    else
        echo -e "${YELLOW}安装 docker-compose-plugin...${NC}"
        detect_os
        case "$OS" in
            ubuntu|debian) apt-get update && apt-get install -y docker-compose-plugin || true ;;
            centos|rhel|rocky|almalinux) dnf install -y docker-compose-plugin || true ;;
        esac
        DOCKER_COMPOSE_CMD="docker compose"
    fi

    # 4. 配置 .env
    setup_env

    # 5. 选择网络与 SSL 模式
    echo ""
    echo -e "${CYAN}请选择 Docker 架构网络方案：${NC}"
    echo -e "  ${BOLD}1)${NC} 独立 HTTPS 端口模式 (由 Docker 内置 Nginx 监听 ${BOLD}8443${NC} 并直接复用已有的 SSL 证书) ${GREEN}[推荐]${NC}"
    echo -e "  ${BOLD}2)${NC} 仅后端容器模式 (只运行 marksix-app 监听本地 8000，由现有 cdn-cf-optimizer/Nginx 在 443 上反代)"
    read -r -p "请输入选项 (1 或 2, 默认: 1): " DOCKER_MODE
    DOCKER_MODE=${DOCKER_MODE:-1}

    PUBLIC_IP=$(curl -s4 ifconfig.me || curl -s4 icanhazip.com || echo "your-server-ip")

    if [ "$DOCKER_MODE" = "1" ]; then
        read -r -p "请输入 Docker HTTPS 监听端口 (默认: 8443): " HTTPS_PORT
        HTTPS_PORT=${HTTPS_PORT:-8443}

        read -r -p "请输入访问域名 (如与 cdn-cf-optimizer 相同的域名): " DOMAIN_NAME
        while [ -z "$DOMAIN_NAME" ]; do
            read -r -p "域名不能为空，请重新输入: " DOMAIN_NAME
        done

        echo -e "${YELLOW}请提供服务器上已有的证书路径 (例如 cdn-cf-optimizer 证书)：${NC}"
        DETECTED_CERT=$(ls -t /etc/letsencrypt/live/*/fullchain.pem 2>/dev/null | head -1 || true)
        DETECTED_KEY=$(ls -t /etc/letsencrypt/live/*/privkey.pem 2>/dev/null | head -1 || true)

        read -r -p "SSL 证书路径 fullchain.pem (默认: ${DETECTED_CERT:-/etc/letsencrypt/live/yourdomain/fullchain.pem}): " SSL_CERT
        SSL_CERT=${SSL_CERT:-$DETECTED_CERT}

        read -r -p "SSL 私钥路径 privkey.pem (默认: ${DETECTED_KEY:-/etc/letsencrypt/live/yourdomain/privkey.pem}): " SSL_KEY
        SSL_KEY=${SSL_KEY:-$DETECTED_KEY}

        # 确保证书文件存在
        while [ ! -f "$SSL_CERT" ] || [ ! -f "$SSL_KEY" ]; do
            echo -e "${RED}[ERROR] 指定的证书文件或私钥不存在，请重新输入！${NC}"
            read -r -p "SSL 证书路径: " SSL_CERT
            read -r -p "SSL 私钥路径: " SSL_KEY
        done

        # 生成 Docker Nginx 配置
        # 将证书所在的根目录挂载进容器
        CERT_DIR="$(cd "$(dirname "$SSL_CERT")/../.." && pwd)"
        if [[ "$CERT_DIR" != "/etc/letsencrypt" ]]; then
            CERT_DIR="$(cd "$(dirname "$SSL_CERT")" && pwd)"
        fi

        # 在容器内部的路径
        CONTAINER_CERT="$SSL_CERT"
        CONTAINER_KEY="$SSL_KEY"

        sed -e "s|__HTTPS_PORT__|$HTTPS_PORT|g" \
            -e "s|__DOMAIN_NAME__|$DOMAIN_NAME|g" \
            -e "s|__SSL_CERT__|$CONTAINER_CERT|g" \
            -e "s|__SSL_KEY__|$CONTAINER_KEY|g" \
            nginx/docker-nginx.conf.tmpl > nginx/docker-nginx.conf

        export HTTPS_PORT="$HTTPS_PORT"
        export SSL_CERTS_DIR="$CERT_DIR"

        echo -e "${BLUE}[*] 正在构建并启动 Docker 容器 (含 HTTPS 反代)...${NC}"
        $DOCKER_COMPOSE_CMD --profile ssl up -d --build

        FINAL_URL="https://${DOMAIN_NAME}:${HTTPS_PORT}"

        # 放行防火墙端口
        if command -v ufw >/dev/null 2>&1 && ufw status | grep -q active; then
            ufw allow "$HTTPS_PORT"/tcp || true
        elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
            firewall-cmd --permanent --add-port="$HTTPS_PORT"/tcp || true
            firewall-cmd --reload || true
        fi

    else
        echo -e "${BLUE}[*] 正在启动仅后端容器 (端口 8000)...${NC}"
        $DOCKER_COMPOSE_CMD up -d --build
        FINAL_URL="http://${PUBLIC_IP}:8000"
    fi

    echo ""
    echo -e "${GREEN}${BOLD}==========================================================${NC}"
    echo -e "${GREEN}${BOLD}🎉 Docker 容器部署成功！${NC}"
    echo -e "${GREEN}${BOLD}==========================================================${NC}"
    echo -e "🌐 访问地址: ${CYAN}${BOLD}${FINAL_URL}${NC}"
    USER=$(grep "^AUTH_USERNAME=" "$APP_DIR/.env" | cut -d '=' -f2)
    PASS=$(grep "^AUTH_PASSWORD=" "$APP_DIR/.env" | cut -d '=' -f2)
    echo -e "👤 登录账号: ${YELLOW}${BOLD}${USER}${NC}"
    echo -e "🔑 登录密码: ${YELLOW}${BOLD}${PASS}${NC}"
    echo ""
    echo -e "${BOLD}Docker 容器维护命令：${NC}"
    echo -e "  • 查看运行状态: ${CYAN}docker compose ps${NC}"
    echo -e "  • 查看实时日志: ${CYAN}docker compose logs -f${NC}"
    echo -e "  • 重启容器服务: ${CYAN}docker compose restart${NC}"
    echo -e "  • 停止容器服务: ${CYAN}docker compose down${NC}"
    if [ "$DOCKER_MODE" = "2" ]; then
        echo ""
        echo -e "${YELLOW}💡 提示：若希望通过现有的 cdn-cf-optimizer (443) 访问，只需在其 Nginx 中追加：${NC}"
        echo -e "   location /marksix/ { proxy_pass http://127.0.0.1:8000/; }"
    fi
    echo -e "${GREEN}${BOLD}==========================================================${NC}"
}

# ------------------------------------------------------------------------------
# 路线 B: 传统 Systemd 本机部署 (备用)
# ------------------------------------------------------------------------------
deploy_systemd() {
    check_root
    echo -e "${BLUE}[1/5] 安装系统依赖...${NC}"
    detect_os
    case "$OS" in
        ubuntu|debian)
            apt-get update -y && apt-get install -y python3 python3-pip python3-venv git curl nginx
            ;;
        centos|rhel|rocky|almalinux)
            yum install -y epel-release || dnf install -y epel-release || true
            yum install -y python3 python3-pip git curl nginx || dnf install -y python3 python3-pip git curl nginx
            ;;
    esac

    setup_env

    echo -e "${BLUE}[2/5] 创建 Python 虚拟环境...${NC}"
    cd "$APP_DIR"
    [ -d "venv" ] || python3 -m venv venv
    ./venv/bin/pip install --upgrade pip -q
    ./venv/bin/pip install -r requirements.txt -q

    echo -e "${BLUE}[3/5] 注册 Systemd 守护进程...${NC}"
    APP_PORT=$(grep "^PORT=" "$APP_DIR/.env" | cut -d '=' -f2)
    APP_PORT=${APP_PORT:-8000}

    cat <<EOF > "/etc/systemd/system/${SERVICE_NAME}.service"
[Unit]
Description=Hong Kong Mark Six Hub Dashboard Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port $APP_PORT --workers 2
Restart=always
RestartSec=3
EnvironmentFile=$APP_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    echo -e "${GREEN}[✓] Systemd 服务已启动在 127.0.0.1:${APP_PORT}！${NC}"
}

# ------------------------------------------------------------------------------
# 子命令与交互菜单
# ------------------------------------------------------------------------------
case "$1" in
    docker)
        deploy_docker
        ;;
    systemd)
        deploy_systemd
        ;;
    status)
        if command -v docker >/dev/null 2>&1 && docker compose ps >/dev/null 2>&1; then
            docker compose ps
        else
            systemctl status "$SERVICE_NAME"
        fi
        ;;
    restart)
        check_root
        if command -v docker >/dev/null 2>&1 && docker compose ps >/dev/null 2>&1; then
            docker compose restart
            echo -e "${GREEN}[✓] Docker 容器已重启！${NC}"
        else
            systemctl restart "$SERVICE_NAME"
            systemctl reload nginx 2>/dev/null || true
            echo -e "${GREEN}[✓] Systemd 服务已重启！${NC}"
        fi
        ;;
    stop)
        check_root
        if command -v docker >/dev/null 2>&1; then
            docker compose down 2>/dev/null || true
        fi
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        echo -e "${YELLOW}[!] 服务已停止。${NC}"
        ;;
    logs)
        if command -v docker >/dev/null 2>&1; then
            docker compose logs -f
        else
            journalctl -u "$SERVICE_NAME" -f
        fi
        ;;
    update)
        check_root
        echo -e "${BLUE}[*] 正在拉取 GitHub 最新代码...${NC}"
        cd "$APP_DIR"
        git pull
        if command -v docker >/dev/null 2>&1 && docker compose ps >/dev/null 2>&1; then
            docker compose up -d --build
        else
            ./venv/bin/pip install -r requirements.txt -q
            systemctl restart "$SERVICE_NAME"
        fi
        echo -e "${GREEN}[✓] 已平滑更新至最新版本！${NC}"
        ;;
    *)
        check_root
        echo -e "${CYAN}请选择您希望使用的部署模式：${NC}"
        echo -e "  ${BOLD}1)${NC} 🐳 ${GREEN}Docker 容器化部署 (推荐，环境隔离，一键启停)${NC}"
        echo -e "  ${BOLD}2)${NC} ⚙️ 传统 Systemd 服务部署 (Python 虚拟环境 + 本机服务)"
        echo -e "  ${BOLD}3)${NC} 🧹 停止并清理当前正在运行的所有实例"
        read -r -p "请输入数字 [1-3] (默认: 1): " CHOICE
        CHOICE=${CHOICE:-1}

        case "$CHOICE" in
            1) deploy_docker ;;
            2) deploy_systemd ;;
            3)
                stop_old_systemd
                docker compose down 2>/dev/null || true
                echo -e "${GREEN}[✓] 清理完成！${NC}"
                ;;
            *) deploy_docker ;;
        esac
        ;;
esac
