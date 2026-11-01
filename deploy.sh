#!/usr/bin/env bash
# ==============================================================================
# 香港六合彩 (Mark Six) 即时看板与真随机选号工具 - 服务器一键部署与运维脚本
# 支持: Docker 容器化部署 (推荐) & 传统 Systemd 本机部署
# 智能特性: 全自动深度探测宿主机/Docker (Caddy/Certbot/acme.sh) SSL 证书与域名
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

# 全自动深度扫描探测服务器已有 SSL 证书 (包含 Docker Caddy/Nginx卷, Certbot, acme.sh)
auto_detect_certificates() {
    echo -e "${BLUE}[*] 正在全盘深度探测服务器上已有的 SSL 证书 (含 Docker Caddy/Certbot)...${NC}"
    
    DETECTED_CERTS=()
    DETECTED_KEYS=()
    DETECTED_DOMAINS=()

    # 1. 扫描 Docker 命名数据卷 (例如 cdn-cf-optimizer 的 caddy_data 数据卷)
    if [ -d "/var/lib/docker/volumes" ]; then
        while IFS= read -r cert; do
            dir=$(dirname "$cert")
            key=$(find "$dir" -maxdepth 1 -name "*.key" 2>/dev/null | head -1 || true)
            if [ -n "$key" ] && [ -f "$key" ]; then
                DETECTED_CERTS+=("$cert")
                DETECTED_KEYS+=("$key")
                # 尝试从证书读取域名
                domain=""
                if command -v openssl >/dev/null 2>&1; then
                    domain=$(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null | grep -o 'DNS:[^, ]*' | head -1 | cut -d':' -f2 || true)
                fi
                [ -z "$domain" ] && domain=$(basename "$cert" .crt)
                DETECTED_DOMAINS+=("$domain")
            fi
        done < <(find /var/lib/docker/volumes -type f \( -name "*.crt" -o -name "fullchain.pem" \) 2>/dev/null)
    fi

    # 2. 扫描 Let's Encrypt / Certbot 官方默认路径
    if [ -d "/etc/letsencrypt/live" ]; then
        for cert in /etc/letsencrypt/live/*/fullchain.pem; do
            if [ -f "$cert" ]; then
                dir=$(dirname "$cert")
                key="$dir/privkey.pem"
                if [ -f "$key" ]; then
                    DETECTED_CERTS+=("$cert")
                    DETECTED_KEYS+=("$key")
                    domain=$(basename "$dir")
                    DETECTED_DOMAINS+=("$domain")
                fi
            fi
        done
    fi

    # 3. 扫描本机 Caddy 路径
    for caddy_dir in /var/lib/caddy/.local/share/caddy/certificates ~/.local/share/caddy/certificates; do
        if [ -d "$caddy_dir" ]; then
            while IFS= read -r cert; do
                dir=$(dirname "$cert")
                key=$(find "$dir" -maxdepth 1 -name "*.key" 2>/dev/null | head -1 || true)
                if [ -n "$key" ] && [ -f "$key" ]; then
                    DETECTED_CERTS+=("$cert")
                    DETECTED_KEYS+=("$key")
                    domain=$(basename "$cert" .crt)
                    DETECTED_DOMAINS+=("$domain")
                fi
            done < <(find "$caddy_dir" -type f -name "*.crt" 2>/dev/null)
        fi
    done

    # 4. 扫描 acme.sh 路径
    if [ -d "$HOME/.acme.sh" ]; then
        for cert in "$HOME/.acme.sh"/*/*.cer; do
            if [ -f "$cert" ] && [[ "$cert" != *"ca.cer"* ]]; then
                dir=$(dirname "$cert")
                key=$(find "$dir" -maxdepth 1 -name "*.key" 2>/dev/null | head -1 || true)
                if [ -n "$key" ] && [ -f "$key" ]; then
                    DETECTED_CERTS+=("$cert")
                    DETECTED_KEYS+=("$key")
                    domain=$(basename "$dir")
                    DETECTED_DOMAINS+=("$domain")
                fi
            fi
        done
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
# 路线 A: Docker 容器化部署
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

    # 5. 自动探测已有证书
    auto_detect_certificates

    echo ""
    echo -e "${CYAN}请选择 Docker 架构网络方案：${NC}"
    echo -e "  ${BOLD}1)${NC} 独立 HTTPS 端口模式 (由 Docker 监听 ${BOLD}8443${NC} 并自动复用已有 SSL 证书) ${GREEN}[推荐]${NC}"
    echo -e "  ${BOLD}2)${NC} 仅后端容器模式 (只运行 marksix-app 监听本地 8000，由现有 cdn-cf-optimizer 反代)"
    read -r -p "请输入选项 (1 或 2, 默认: 1): " DOCKER_MODE
    DOCKER_MODE=${DOCKER_MODE:-1}

    PUBLIC_IP=$(curl -s4 ifconfig.me || curl -s4 icanhazip.com || echo "your-server-ip")

    if [ "$DOCKER_MODE" = "1" ]; then
        read -r -p "请输入 Docker HTTPS 监听端口 (默认: 8443): " HTTPS_PORT
        HTTPS_PORT=${HTTPS_PORT:-8443}

        CHOSEN_CERT=""
        CHOSEN_KEY=""
        CHOSEN_DOMAIN=""

        NUM_CERTS=${#DETECTED_CERTS[@]}
        if [ "$NUM_CERTS" -gt 0 ]; then
            echo -e "${GREEN}${BOLD}[✓] 自动探测到服务器上已有 $NUM_CERTS 处可用证书：${NC}"
            for i in "${!DETECTED_CERTS[@]}"; do
                echo -e "  ${BOLD}$((i+1)))${NC} 域名: ${CYAN}${DETECTED_DOMAINS[$i]}${NC}"
                echo -e "      证书: ${DETECTED_CERTS[$i]}"
            done

            read -r -p "请选择要复用的证书编号 [1-$NUM_CERTS] (默认: 1): " CERT_INDEX
            CERT_INDEX=${CERT_INDEX:-1}
            idx=$((CERT_INDEX - 1))

            CHOSEN_CERT="${DETECTED_CERTS[$idx]}"
            CHOSEN_KEY="${DETECTED_KEYS[$idx]}"
            CHOSEN_DOMAIN="${DETECTED_DOMAINS[$idx]}"

            echo -e "${GREEN}[✓] 已选定证书: $CHOSEN_CERT${NC}"
            echo -e "${GREEN}[✓] 自动匹配域名: ${BOLD}$CHOSEN_DOMAIN${NC}"
        else
            echo -e "${YELLOW}[!] 未能自动检索到现有证书，请手动输入：${NC}"
            read -r -p "请输入访问域名: " CHOSEN_DOMAIN
            read -r -p "请输入 fullchain.pem/crt 完整路径: " CHOSEN_CERT
            read -r -p "请输入 privkey.pem/key 完整路径: " CHOSEN_KEY
        fi

        # 挂载目录与文件名准备
        CERT_DIR="$(cd "$(dirname "$CHOSEN_CERT")" && pwd)"
        CERT_FILE="$(basename "$CHOSEN_CERT")"
        KEY_FILE="$(basename "$CHOSEN_KEY")"

        # 生成 Docker Nginx 配置文件
        sed -e "s|__HTTPS_PORT__|$HTTPS_PORT|g" \
            -e "s|__DOMAIN_NAME__|$CHOSEN_DOMAIN|g" \
            -e "s|__SSL_CERT__|/etc/nginx/ssl/$CERT_FILE|g" \
            -e "s|__SSL_KEY__|/etc/nginx/ssl/$KEY_FILE|g" \
            nginx/docker-nginx.conf.tmpl > nginx/docker-nginx.conf

        export HTTPS_PORT="$HTTPS_PORT"
        export SSL_CERTS_DIR="$CERT_DIR"

        echo -e "${BLUE}[*] 正在构建并启动 Docker 容器 (FastAPI + Nginx HTTPS 8443)...${NC}"
        $DOCKER_COMPOSE_CMD --profile ssl up -d --build

        FINAL_URL="https://${CHOSEN_DOMAIN}:${HTTPS_PORT}"

        # 防火墙放行
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
    if [ "$DOCKER_MODE" = "1" ]; then
        echo ""
        echo -e "${YELLOW}⚠️ 重要提示：若为云服务器 (如甲骨文 Oracle Cloud/阿里云/腾讯云)，请务必在控制台【安全组/安全列表】添加入站规则放行 TCP $HTTPS_PORT 端口！${NC}"
    else
        echo ""
        echo -e "${YELLOW}💡 提示：若希望通过现有的 cdn-cf-optimizer (443) 访问，只需在其 Caddyfile/Nginx 中追加：${NC}"
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
