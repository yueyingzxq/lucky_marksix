#!/usr/bin/env bash
# ==============================================================================
# 香港六合彩 (Mark Six) 即时看板与真随机选号工具 - 服务器一键部署与运维脚本
# 支持系统: Ubuntu 20.04+, Debian 11+, CentOS 8+, Rocky Linux, AlmaLinux
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
DEFAULT_PORT=8000

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

        # 写入 .env 文件
        sed -i "s/^AUTH_USERNAME=.*/AUTH_USERNAME=$INPUT_USER/" .env || true
        sed -i "s/^AUTH_PASSWORD=.*/AUTH_PASSWORD=$INPUT_PASS/" .env || true
        
        # 确保生成唯一的 SECRET_KEY
        RAND_KEY=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)
        sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$RAND_KEY/" .env || true

        echo -e "${GREEN}[✓] 登录凭证已配置！用户名: ${BOLD}$INPUT_USER${NC}${GREEN}，密码: ${BOLD}$INPUT_PASS${NC}"
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
    
    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Hong Kong Mark Six Hub Dashboard Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 2
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
        echo -e "${GREEN}[✓] ${SERVICE_NAME} 服务启动成功！${NC}"
    else
        echo -e "${RED}[ERROR] 服务启动失败，请检查日志: journalctl -u ${SERVICE_NAME} -n 20${NC}"
        exit 1
    fi
}

# 配置 Nginx 与 HTTPS (SSL)
setup_nginx_ssl() {
    echo -e "${BLUE}[5/5] 配置 Nginx 反向代理与 SSL 证书...${NC}"

    read -r -p "是否绑定域名并配置 HTTPS (SSL)？(y/n, 默认: y): " USE_DOMAIN
    USE_DOMAIN=${USE_DOMAIN:-y}

    PUBLIC_IP=$(curl -s4 ifconfig.me || curl -s4 icanhazip.com || echo "your-server-ip")

    if [[ "$USE_DOMAIN" =~ ^[Yy]$ ]]; then
        read -r -p "请输入您的域名 (例如 marksix.yourdomain.com): " DOMAIN_NAME
        while [ -z "$DOMAIN_NAME" ]; do
            read -r -p "域名不能为空，请重新输入: " DOMAIN_NAME
        done

        # 临时创建 HTTP 验证配置
        NGINX_CONF="/etc/nginx/conf.d/${SERVICE_NAME}.conf"
        [ -d /etc/nginx/sites-available ] && NGINX_CONF="/etc/nginx/sites-available/${SERVICE_NAME}.conf"

        cat <<EOF > "$NGINX_CONF"
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN_NAME;

    location / {
        proxy_pass http://127.0.0.1:8000;
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
            # 移除默认 default 站点防止冲突
            rm -f /etc/nginx/sites-enabled/default
        fi

        nginx -t && systemctl reload nginx

        echo -e "${CYAN}正在通过 Let's Encrypt / Certbot 申请并配置 SSL 证书...${NC}"
        read -r -p "请输入用于证书接收告警的邮箱 (可直接回车跳过): " USER_EMAIL

        if [ -n "$USER_EMAIL" ]; then
            certbot --nginx -d "$DOMAIN_NAME" --non-interactive --agree-tos -m "$USER_EMAIL" --redirect || true
        else
            certbot --nginx -d "$DOMAIN_NAME" --non-interactive --agree-tos --register-unsafely-without-email --redirect || true
        fi

        nginx -t && systemctl reload nginx
        FINAL_URL="https://$DOMAIN_NAME"
    else
        # 仅 IP 模式 (HTTP 80)
        NGINX_CONF="/etc/nginx/conf.d/${SERVICE_NAME}.conf"
        [ -d /etc/nginx/sites-available ] && NGINX_CONF="/etc/nginx/sites-available/${SERVICE_NAME}.conf"

        cat <<EOF > "$NGINX_CONF"
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
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
            rm -f /etc/nginx/sites-enabled/default
        fi

        nginx -t && systemctl reload nginx
        FINAL_URL="http://$PUBLIC_IP"
    fi

    echo -e "${GREEN}[✓] Nginx 反向代理配置完成！${NC}"
}

# 部署完成报告
show_summary() {
    # 从 .env 读取账号密码
    USER=$(grep "^AUTH_USERNAME=" "$APP_DIR/.env" | cut -d '=' -f2)
    PASS=$(grep "^AUTH_PASSWORD=" "$APP_DIR/.env" | cut -d '=' -f2)

    echo ""
    echo -e "${GREEN}${BOLD}==========================================================${NC}"
    echo -e "${GREEN}${BOLD}🎉 香港六合彩 (Mark Six) 个人即时看板与选号工具 部署成功！${NC}"
    echo -e "${GREEN}${BOLD}==========================================================${NC}"
    echo -e "🌐 访问地址: ${CYAN}${BOLD}${FINAL_URL:-http://$PUBLIC_IP}${NC}"
    echo -e "👤 登录账号: ${YELLOW}${BOLD}${USER}${NC}"
    echo -e "🔑 登录密码: ${YELLOW}${BOLD}${PASS}${NC}"
    echo -e "📌 本地服务端口: ${BOLD}127.0.0.1:8000${NC}"
    echo ""
    echo -e "${BOLD}常用系统维护命令：${NC}"
    echo -e "  • 重启服务:   ${CYAN}sudo systemctl restart ${SERVICE_NAME}${NC}"
    echo -e "  • 停止服务:   ${CYAN}sudo systemctl stop ${SERVICE_NAME}${NC}"
    echo -e "  • 查看状态:   ${CYAN}sudo systemctl status ${SERVICE_NAME}${NC}"
    echo -e "  • 查看实时日志: ${CYAN}sudo journalctl -u ${SERVICE_NAME} -f${NC}"
    echo -e "  • 更新最新代码: ${CYAN}sudo bash deploy.sh update${NC}"
    echo -e "${GREEN}${BOLD}==========================================================${NC}"
}

# 子命令分支
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
    echo -e "${GREEN}[✓] Docker 容器已启动在端口 8000！${NC}"
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
