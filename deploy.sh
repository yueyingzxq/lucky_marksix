#!/usr/bin/env bash
# ==============================================================================
# 香港六合彩 (Mark Six) 即时看板与真随机选号工具 - 服务器一键部署与运维脚本
# 支持系统: Ubuntu 20.04+, Debian 11+, CentOS 8+, Rocky Linux, AlmaLinux
# 支持自定义 HTTPS 端口 (如 8443) 与复用已有 SSL 证书 (如 cdn-cf-optimizer 证书)
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
echo "   🎲 香港六合彩 (Mark Six) 个人即时看板与选号工具部署脚本"
echo "=========================================================="
echo -e "${NC}"

# 权限检测
check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}[ERROR] 请使用 root 权限或 sudo 运行此脚本！${NC}"
        echo -e "示例: ${YELLOW}sudo bash deploy.sh${NC}"
        exit 1
    fi
}

# 检测包管理器
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
    else
        echo -e "${RED}[ERROR] 无法识别当前操作系统！${NC}"
        exit 1
    fi
}

# 检测端口是否被占用
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

# 安装基础依赖
install_dependencies() {
    echo -e "${BLUE}[1/5] 正在安装系统基础依赖 (Python3, Nginx, Git, Certbot)...${NC}"
    detect_os
    case "$OS" in
        ubuntu|debian)
            apt-get update -y
            apt-get install -y python3 python3-pip python3-venv git curl nginx certbot python3-certbot-nginx
            ;;
        centos|rhel|rocky|almalinux)
            yum install -y epel-release || dnf install -y epel-release || true
            yum install -y python3 python3-pip git curl nginx certbot python3-certbot-nginx || dnf install -y python3 python3-pip git curl nginx certbot python3-certbot-nginx
            ;;
        *)
            echo -e "${YELLOW}[WARN] 未知操作系统发行版: $OS，尝试通用 apt/yum...${NC}"
            if command -v apt-get >/dev/null 2>&1; then
                apt-get update -y && apt-get install -y python3 python3-pip python3-venv git curl nginx certbot python3-certbot-nginx
            elif command -v dnf >/dev/null 2>&1; then
                dnf install -y python3 python3-pip git curl nginx certbot python3-certbot-nginx
            elif command -v yum >/dev/null 2>&1; then
                yum install -y python3 python3-pip git curl nginx certbot python3-certbot-nginx
            fi
            ;;
    esac
    echo -e "${GREEN}[✓] 系统依赖安装完成！${NC}"
}

# 配置环境与账号密码
setup_env() {
    echo -e "${BLUE}[2/5] 配置应用环境变量与登录凭证...${NC}"
    cd "$APP_DIR"
    
    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            cp .env.example .env
        else
            cat <<EOF > .env
HOST=127.0.0.1
PORT=8000
AUTH_ENABLED=true
AUTH_USERNAME=admin
AUTH_PASSWORD=marksix888
SECRET_KEY=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24)
CACHE_TTL_SECONDS=3600
EOF
        fi

        echo -e "${YELLOW}检测到首次部署，请设置后台登录凭证：${NC}"
        read -r -p "请输入管理员用户名 (默认: admin): " INPUT_USER
        INPUT_USER=${INPUT_USER:-admin}

        # 生成默认随机安全密码
        DEF_PASS=$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 10)
        read -r -p "请输入管理员密码 (默认自动生成: $DEF_PASS): " INPUT_PASS
        INPUT_PASS=${INPUT_PASS:-$DEF_PASS}

        # 检测本地 FastAPI 8000 端口是否被占
        FASTAPI_PORT=$DEFAULT_FASTAPI_PORT
        if is_port_in_use "$FASTAPI_PORT"; then
            echo -e "${YELLOW}[!] 检测到本地 $FASTAPI_PORT 端口被占用，请指定其他本地后端端口${NC}"
            read -r -p "请输入本地 FastAPI 端口 (推荐 8001 或 8080): " FASTAPI_PORT
            FASTAPI_PORT=${FASTAPI_PORT:-8001}
        fi

        # 写入 .env 文件
        sed -i "s/^AUTH_USERNAME=.*/AUTH_USERNAME=$INPUT_USER/" .env || true
        sed -i "s/^AUTH_PASSWORD=.*/AUTH_PASSWORD=$INPUT_PASS/" .env || true
        sed -i "s/^PORT=.*/PORT=$FASTAPI_PORT/" .env || true
        
        # 确保生成唯一的 SECRET_KEY
        RAND_KEY=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)
        sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$RAND_KEY/" .env || true

        echo -e "${GREEN}[✓] 登录凭证与端口已配置！用户名: ${BOLD}$INPUT_USER${NC}${GREEN}，密码: ${BOLD}$INPUT_PASS${NC}，后端端口: ${BOLD}$FASTAPI_PORT${NC}"
    else
        echo -e "${GREEN}[✓] 已存在 .env 配置文件，保留当前配置。${NC}"
    fi
}

# 设置 Python 虚拟环境与依赖
setup_python_venv() {
    echo -e "${BLUE}[3/5] 构建 Python 虚拟环境并安装核心库...${NC}"
    cd "$APP_DIR"
    
    if [ ! -d "venv" ]; then
        python3 -m venv venv
    fi

    ./venv/bin/pip install --upgrade pip -q
    ./venv/bin/pip install -r requirements.txt -q
    echo -e "${GREEN}[✓] Python 虚拟环境及依赖安装完毕！${NC}"
}

# 创建并启动 Systemd 服务
setup_systemd() {
    echo -e "${BLUE}[4/5] 注册并启动 Systemd 系统守护进程...${NC}"
    
    APP_PORT=$(grep "^PORT=" "$APP_DIR/.env" | cut -d '=' -f2)
    APP_PORT=${APP_PORT:-8000}

    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    cat <<EOF > "$SERVICE_FILE"
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
    
    sleep 2
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        echo -e "${GREEN}[✓] ${SERVICE_NAME} 服务启动成功 (127.0.0.1:$APP_PORT)！${NC}"
    else
        echo -e "${RED}[ERROR] 服务启动失败，请检查日志: journalctl -u ${SERVICE_NAME} -n 20${NC}"
        exit 1
    fi
}

# 配置 Nginx 与 HTTPS (支持自定义端口与复用证书)
setup_nginx_ssl() {
    echo -e "${BLUE}[5/5] 配置 Nginx 反向代理与 SSL 证书...${NC}"

    APP_PORT=$(grep "^PORT=" "$APP_DIR/.env" | cut -d '=' -f2)
    APP_PORT=${APP_PORT:-8000}
    PUBLIC_IP=$(curl -s4 ifconfig.me || curl -s4 icanhazip.com || echo "your-server-ip")

    read -r -p "是否需要配置 HTTPS (SSL) 加密访问？(y/n, 默认: y): " USE_HTTPS
    USE_HTTPS=${USE_HTTPS:-y}

    if [[ "$USE_HTTPS" =~ ^[Yy]$ ]]; then
        # 检测 443 端口是否已被别的服务(如 cdn-cf-optimizer)占用
        DEFAULT_HTTPS_PORT=443
        if is_port_in_use 443; then
            echo -e "${YELLOW}[!] 检测到服务器 443 端口已被占用 (如 cdn-cf-optimizer / 现有 Web 服务)！${NC}"
            echo -e "${CYAN}建议为六合彩看板分配独立 HTTPS 端口 (如 8443、9443 等)${NC}"
            DEFAULT_HTTPS_PORT=8443
        fi

        read -r -p "请输入六合彩 HTTPS 监听端口 (默认: $DEFAULT_HTTPS_PORT): " HTTPS_PORT
        HTTPS_PORT=${HTTPS_PORT:-$DEFAULT_HTTPS_PORT}

        read -r -p "请输入访问域名 (如 m6.yourdomain.com 或原 cdn 域名): " DOMAIN_NAME
        while [ -z "$DOMAIN_NAME" ]; do
            read -r -p "域名不能为空，请重新输入: " DOMAIN_NAME
        done

        echo ""
        echo -e "${CYAN}请选择 SSL 证书获取方式：${NC}"
        echo -e "  ${BOLD}1)${NC} 复用服务器已有的证书 (如 cdn-cf-optimizer 的证书文件) ${GREEN}[推荐]${NC}"
        echo -e "  ${BOLD}2)${NC} 使用 Certbot 全自动申请全新 Let's Encrypt 证书"
        read -r -p "请输入选项 (1 或 2, 默认: 1): " CERT_MODE
        CERT_MODE=${CERT_MODE:-1}

        SSL_CERT=""
        SSL_KEY=""

        if [ "$CERT_MODE" = "1" ]; then
            echo -e "${YELLOW}请提供已有证书与私钥路径 (以 .pem 或 .crt 结尾)：${NC}"
            DETECTED_CERT=$(ls -t /etc/letsencrypt/live/*/fullchain.pem 2>/dev/null | head -1 || true)
            DETECTED_KEY=$(ls -t /etc/letsencrypt/live/*/privkey.pem 2>/dev/null | head -1 || true)

            read -r -p "SSL 证书路径 fullchain.pem (默认: ${DETECTED_CERT:-/etc/letsencrypt/live/yourdomain/fullchain.pem}): " SSL_CERT
            SSL_CERT=${SSL_CERT:-$DETECTED_CERT}

            read -r -p "SSL 私钥路径 privkey.pem (默认: ${DETECTED_KEY:-/etc/letsencrypt/live/yourdomain/privkey.pem}): " SSL_KEY
            SSL_KEY=${SSL_KEY:-$DETECTED_KEY}

            while [ ! -f "$SSL_CERT" ] || [ ! -f "$SSL_KEY" ]; do
                echo -e "${RED}[ERROR] 指定的证书文件或私钥不存在，请重新输入！${NC}"
                read -r -p "SSL 证书路径: " SSL_CERT
                read -r -p "SSL 私钥路径: " SSL_KEY
            done
            echo -e "${GREEN}[✓] 已成功载入现有证书: $SSL_CERT${NC}"
        else
            echo -e "${CYAN}正在调用 Certbot 申请证书...${NC}"
            read -r -p "请输入告警通知邮箱 (直接回车跳过): " USER_EMAIL
            if [ -n "$USER_EMAIL" ]; then
                certbot certonly --standalone -d "$DOMAIN_NAME" --non-interactive --agree-tos -m "$USER_EMAIL" || true
            else
                certbot certonly --standalone -d "$DOMAIN_NAME" --non-interactive --agree-tos --register-unsafely-without-email || true
            fi
            SSL_CERT="/etc/letsencrypt/live/$DOMAIN_NAME/fullchain.pem"
            SSL_KEY="/etc/letsencrypt/live/$DOMAIN_NAME/privkey.pem"
        fi

        # 写入 Nginx 配置
        NGINX_CONF="/etc/nginx/conf.d/${SERVICE_NAME}.conf"
        [ -d /etc/nginx/sites-available ] && NGINX_CONF="/etc/nginx/sites-available/${SERVICE_NAME}.conf"

        cat <<EOF > "$NGINX_CONF"
# 香港六合彩 (Mark Six) Nginx 反向代理配置 (独立端口 HTTPS)
server {
    listen $HTTPS_PORT ssl http2;
    listen [::]:$HTTPS_PORT ssl http2;
    server_name $DOMAIN_NAME;

    # SSL 证书配置 (复用已有证书)
    ssl_certificate $SSL_CERT;
    ssl_certificate_key $SSL_KEY;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:10m;

    # 安全响应头
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;

    # 开启 Gzip
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF
        if [ -d /etc/nginx/sites-enabled ]; then
            ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/${SERVICE_NAME}.conf"
        fi

        nginx -t && systemctl reload nginx

        PORT_SUFFIX=""
        [ "$HTTPS_PORT" != "443" ] && PORT_SUFFIX=":$HTTPS_PORT"
        FINAL_URL="https://${DOMAIN_NAME}${PORT_SUFFIX}"

        if [ "$HTTPS_PORT" != "443" ]; then
            if command -v ufw >/dev/null 2>&1 && ufw status | grep -q active; then
                ufw allow "$HTTPS_PORT"/tcp || true
            elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
                firewall-cmd --permanent --add-port="$HTTPS_PORT"/tcp || true
                firewall-cmd --reload || true
            fi
            echo -e "${YELLOW}⚠️ 重要提示：若使用的云服务器（如 Oracle Cloud 甲骨文、阿里云、腾讯云等），请务必在控制台【安全组/安全列表】中添加入站规则放行 TCP $HTTPS_PORT 端口！${NC}"
        fi

    else
        read -r -p "请输入对外 HTTP 监听端口 (默认: 8080): " HTTP_PORT
        HTTP_PORT=${HTTP_PORT:-8080}

        NGINX_CONF="/etc/nginx/conf.d/${SERVICE_NAME}.conf"
        [ -d /etc/nginx/sites-available ] && NGINX_CONF="/etc/nginx/sites-available/${SERVICE_NAME}.conf"

        cat <<EOF > "$NGINX_CONF"
server {
    listen $HTTP_PORT;
    listen [::]:$HTTP_PORT;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
        if [ -d /etc/nginx/sites-enabled ]; then
            ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/${SERVICE_NAME}.conf"
        fi

        nginx -t && systemctl reload nginx
        FINAL_URL="http://${PUBLIC_IP}:${HTTP_PORT}"
    fi

    echo -e "${GREEN}[✓] Nginx 代理配置完成！${NC}"
}

# 部署完成报告
show_summary() {
    USER=$(grep "^AUTH_USERNAME=" "$APP_DIR/.env" | cut -d '=' -f2)
    PASS=$(grep "^AUTH_PASSWORD=" "$APP_DIR/.env" | cut -d '=' -f2)
    APP_PORT=$(grep "^PORT=" "$APP_DIR/.env" | cut -d '=' -f2)

    echo ""
    echo -e "${GREEN}${BOLD}==========================================================${NC}"
    echo -e "${GREEN}${BOLD}🎉 香港六合彩 (Mark Six) 个人即时看板与选号工具 部署成功！${NC}"
    echo -e "${GREEN}${BOLD}==========================================================${NC}"
    echo -e "🌐 访问地址: ${CYAN}${BOLD}${FINAL_URL}${NC}"
    echo -e "👤 登录账号: ${YELLOW}${BOLD}${USER}${NC}"
    echo -e "🔑 登录密码: ${YELLOW}${BOLD}${PASS}${NC}"
    echo -e "📌 本地后端端口: ${BOLD}127.0.0.1:${APP_PORT:-8000}${NC}"
    echo ""
    echo -e "${BOLD}常用系统维护命令：${NC}"
    echo -e "  • 重启服务:     ${CYAN}sudo systemctl restart ${SERVICE_NAME}${NC}"
    echo -e "  • 停止服务:     ${CYAN}sudo systemctl stop ${SERVICE_NAME}${NC}"
    echo -e "  • 查看状态:     ${CYAN}sudo systemctl status ${SERVICE_NAME}${NC}"
    echo -e "  • 查看实时日志: ${CYAN}sudo journalctl -u ${SERVICE_NAME} -f${NC}"
    echo -e "  • 更新最新代码: ${CYAN}sudo bash deploy.sh update${NC}"
    echo -e "${GREEN}${BOLD}==========================================================${NC}"
}

cmd_update() {
    check_root
    echo -e "${BLUE}[*] 正在从 Git 仓库拉取最新代码...${NC}"
    cd "$APP_DIR"
    git pull
    ./venv/bin/pip install -r requirements.txt -q
    systemctl restart "${SERVICE_NAME}"
    echo -e "${GREEN}[✓] 代码更新完成，服务已重启！${NC}"
}

cmd_status() {
    systemctl status "${SERVICE_NAME}"
}

cmd_restart() {
    check_root
    systemctl restart "${SERVICE_NAME}"
    systemctl reload nginx
    echo -e "${GREEN}[✓] 服务与 Nginx 已平滑重启！${NC}"
}

cmd_stop() {
    check_root
    systemctl stop "${SERVICE_NAME}"
    echo -e "${YELLOW}[!] 服务已停止${NC}"
}

cmd_docker() {
    check_root
    echo -e "${BLUE}[*] 使用 Docker Compose 进行容器化部署...${NC}"
    cd "$APP_DIR"
    if ! command -v docker >/dev/null 2>&1; then
        echo -e "${YELLOW}未检测到 Docker，正在安装 Docker 环境...${NC}"
        curl -fsSL https://get.docker.com | bash
    fi
    setup_env
    docker compose up -d --build
    echo -e "${GREEN}[✓] Docker 容器已启动！${NC}"
}

# 主执行入口
case "$1" in
    update)
        cmd_update
        ;;
    status)
        cmd_status
        ;;
    restart)
        cmd_restart
        ;;
    stop)
        cmd_stop
        ;;
    docker)
        cmd_docker
        ;;
    *)
        check_root
        install_dependencies
        setup_env
        setup_python_venv
        setup_systemd
        setup_nginx_ssl
        show_summary
        ;;
esac
