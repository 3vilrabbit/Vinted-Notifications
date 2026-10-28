# Vinted-Notifications

A real-time notification system for Vinted listings that works across all Vinted country domains. Get instant alerts
when items matching your search criteria are posted.

![Vinted-Notifications](https://github.com/user-attachments/assets/f2788511-5a8a-4a8d-8198-a4135081a3d8)

---

## ⚡ Quickstart

If you just want to get started fast with Docker Compose:

```bash
curl -O https://raw.githubusercontent.com/Fuyucch1/Vinted-Notifications/main/docker-compose.yml
docker-compose up -d
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

---

## 📋 Features

- **Web UI**: Manage everything through an intuitive web interface
- **Multi-Country Support**: Works on all Vinted domains regardless of country
- **Real-Time Notifications**: Get instant alerts for new listings
- **Multiple Search Queries**: Monitor multiple search terms simultaneously
- **Country Filtering**: Filter items by seller's country of origin
- **RSS Feed**: Subscribe to your search results with any RSS reader
- **Telegram Integration**: Receive notifications directly in Telegram

---

## 📦 Installation

### Option 1: Docker Run (Simplest)

#### Prerequisites

- Docker installed on your system
- Telegram bot token (for Telegram notifications)

#### Setup with Docker Run

1. **Create directories for persistent data**

   ```bash
   mkdir -p data logs
   ```

2. **Run the container**

   ```bash
   docker run -d \
     --name vinted-notifications \
     -p 8000:8000 \
     -p 8080:8080 \
     -v "$(pwd)/data:/app/data" \
     -v "$(pwd)/logs:/app/logs" \
     --restart unless-stopped \
     fuyucch1/vinted-notifications:latest
   ```

   > **Note**: The volume mounts ensure your data and logs are preserved even if the container is removed or updated.
   The database is stored in the `data` directory, and logs are stored in the `logs` directory.

3. **Access the Web UI**

   Once started, access the Web UI at [http://localhost:8000](http://localhost:8000) to complete the setup.

### Option 2: Docker Compose (Recommended)

#### Prerequisites

- Docker and Docker Compose installed on your system
- Telegram bot token (for Telegram notifications)

#### Setup with Docker Compose

1. **Create a docker-compose.yml file**

   ```yaml
   version: '3.8'

   services:
     vinted-notifications:
       image: fuyucch1/vinted-notifications:latest
       pull_policy: always
       ports:
         - "8000:8000"
         - "8080:8080"
       volumes:
         - VN_data:/app/data
         - VN_logs:/app/logs
       restart: unless-stopped
       read_only: true
       tmpfs:
         - /tmp

   volumes:
     VN_data:
     VN_logs:
   ```

   > **Note**: This configuration uses named volumes (`VN_data` and `VN_logs`) which are managed by Docker. This ensures
   your data and logs are preserved even if the container is removed or updated.

2. **Start the container**

   ```bash
   docker-compose up -d
   ```

3. **Access the Web UI**

   Once started, access the Web UI at [http://localhost:8000](http://localhost:8000) to complete the setup.

### Option 3: Self-Build

#### Prerequisites

- Python 3.11 or higher
- Telegram bot token (for Telegram notifications)

#### Setup

1. **Clone the repository or download the latest release**

   ```bash
   git clone https://github.com/Fuyucch1/Vinted-Notifications.git
   cd Vinted-Notifications
   ```

   Alternatively, download the [latest release](https://github.com/Fuyucch1/Vinted-Notifications/releases/latest) and
   extract it.

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Initial Configuration**

   The application can be configured through the Web UI after starting. However, you can also change the Web UI port in
   the
   `configuration_values.py` file directly.

4. **Run the application**

   ```bash
   python vinted_notifications.py
   ```

   Once started, access the Web UI at [http://localhost:8000](http://localhost:8000) to complete the setup.

## 🚀 Usage

### Web UI

The Web UI is the easiest way to manage the application. Access it at [http://localhost:8000](http://localhost:8000)
after starting the application.

Features available in the Web UI:

- **Dashboard**: Overview of application status and recent items
- **Queries Management**: Add, remove, and view search queries
- **Items Viewing**: Browse and filter items found by the application
- **Allowlist Management**: Filter items by seller's country
- **Configuration**: Set up Telegram bot, RSS feed, and other settings
- **Logs**: View application logs directly from the web interface

### Telegram Commands

After configuring your Telegram bot in the Web UI, you can use the following commands:

- `/add_query query` - Add a search query to monitor
- `/remove_query query_number` - Remove a specific query
- `/remove_query all` - Remove all queries
- `/queries` - List all active queries
- `/hello` - Check if the bot is working
- `/create_allowlist` - Create a country allowlist (will slow down processing)
- `/delete_allowlist` - Delete the country allowlist
- `/add_country XX` - Add a country to the allowlist (ISO3166 format)
- `/remove_country XX` - Remove a country from the allowlist
- `/allowlist` - View the current allowlist

### Query Examples

Queries must be added with a whole link. It works with filters.:

   ```
   /add_query https://www.vinted.fr/catalog?search_text=nike%20shoes&price_to=50&currency=EUR&brand_id[]=53
   ```

### RSS Feed

The RSS feed provides an alternative way to receive notifications. After enabling it in the Web UI, access it
at [http://localhost:8080](http://localhost:8080).

## 👥 Multiple users (shared server)

By default the web interface is a single-user tool. To share one server between several people, go to
**Configuration → Security → Multiple accounts** and create the first administrator. Everything that
exists at that point (queries, items, allowlist, settings) moves to that account, and from then on
everyone signs in.

- Every account has its **own queries, items, allowlist, language/timezone, banned words, message template,
  Telegram bot (token + chat ID) and private RSS feed** (`/feed/<token>`).
- Administrators also manage the server-wide settings (refresh delay, proxies, RSS port...), the accounts and the logs.
  Identical searches from different accounts are fetched from Vinted only once.
- Administrators create accounts from **Accounts**; they can also allow self-service sign-up in Configuration → Server.
- Environment variables: `VN_WEB_PASSWORD` (the single password while accounts are off; it has no effect afterwards),
  `VN_COOKIE_SECURE=1` (serve cookies over HTTPS only) and `VN_TRUST_PROXY=1` (trust `X-Forwarded-*` headers when
  running behind a reverse proxy).
- **Forgotten administrator password?** There is deliberately no master password. On the server, run:

  ```bash
  docker compose exec -u appuser vinted-notifications python reset_admin_password.py --generate
  ```

  (add `--user NAME` for another account, `--enable` to re-enable a disabled one; without `--generate` you type the
  temporary password). It prints a one-time password, signs the account out everywhere and forces a new password at the
  next sign-in. Only someone who can already run commands on the server can do this. Passwords an administrator sets for
  a member are single-use in the same way.
- Serve the interface over HTTPS when it is reachable from the Internet.

## 🔔 Push notifications

Install the web app on your phone (Android: *Install app*; iPhone/iPad, iOS 16.4+: *Share → Add to Home Screen*), open
**Settings → Notifications → Push notifications** and turn them on for that device. Then choose, for each query, whether it
sends a push: the switch is in the form for adding a query and in the one for editing it. Browsers only allow push over
HTTPS, so serve the interface through the bundled reverse proxy (below) or your own.

## 🧹 Sold and removed listings

Stored items are verified in the background, with no setting to manage. Vinted has no endpoint for "is this listing still there", so the listing's own page is asked: a removed listing answers 404 (a cheap HEAD request), a sold one still loads with its purchase state in the page data. Anything unclear is treated as unknown and never deletes anything; a sold listing must be seen sold on two checks before it is dropped. Checks are paced (a small batch every 15 minutes, a pause between requests, each item at most once every 6 hours), and opening an item in the app checks that one item once. Deleted items are remembered, so a later scan does not announce them again. A single item can also be removed with the trash can on its card.

## 🔒 HTTPS and security

```bash
echo "VN_DOMAIN=vinted.example.com" > .env
docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d --build
```

[Caddy](https://caddyserver.com) obtains and renews a Let's Encrypt certificate and becomes the only public entry point
(ports 80/443); the app's own ports then listen on `127.0.0.1` only.

What the interface does for you:

- **Passwords** follow NIST SP 800-63B rev. 4: at least 15 characters (passphrases welcome, any character allowed), refused
  when too common, a simple pattern, containing the username or the app name, or found in known breaches (checked with the
  Have I Been Pwned range API: only a 5-character hash prefix is sent; set `VN_PWNED_CHECK=0` to switch it off).
- **Abuse limits**: per-address rate limits on pages, static files, sign-in/sign-up and sensitive actions, sign-in lockout,
  request-size limit, and a production HTTP server (waitress) with bounded threads, connections and timeouts.
- **Browser protections**: Content-Security-Policy, `frame-ancestors 'none'`, HSTS over HTTPS, SameSite cookies, a same-origin
  check on every state-changing request (CSRF), escaped output, parameterised SQL, and Vinted-only scraper targets.
- **Container**: read-only filesystem, all capabilities dropped except those the entrypoint needs, `no-new-privileges`, a
  process limit and a health check (`/health`).

The rate limiter counts per client address, so run it behind the proxy with `VN_TRUST_PROXY=1` (the Caddy file does).
Put volumetric DDoS protection (your cloud firewall or a CDN) in front of anything that is public.

## ⚙️ Advanced Configuration

### Proxy Support

The application supports using proxies to avoid rate limits. Those are configured in the configuration tab of the Web
UI.

### Custom Notification Format

You can customize the notification message format:

```python
MESSAGE = '''\
🆕 Title: {title}
💶 Price: {price}
🛍️ Brand: {brand}
<a href='{image}'>&#8205;</a>
'''
```

## 🔄 Updating

### Option 1: Docker Run

1. Pull the latest image:
   ```bash
   docker pull fuyucch1/vinted-notifications:latest
   ```
2. Stop and remove the existing container:
   ```bash
   docker stop vinted-notifications
   docker rm vinted-notifications
   ```
3. Run the container again with the same command as in the installation section:
   ```bash
   docker run -d \
     --name vinted-notifications \
     -p 8000:8000 \
     -p 8080:8080 \
     -v "$(pwd)/data:/app/data" \
     -v "$(pwd)/logs:/app/logs" \
     --restart unless-stopped \
     fuyucch1/vinted-notifications:latest
   ```

   > **Important**: This update method preserves your data because of the volume mounts. Your database and logs will be
   maintained across updates as they are stored in the mounted directories.

### Option 2: Docker Compose

1. Pull the latest image:
   ```bash
   docker pull fuyucch1/vinted-notifications:latest
   ```
2. Restart your container:
   ```bash
   docker-compose down
   docker-compose up -d
   ```

   > **Important**: This update method preserves your data because of the named volumes (`VN_data` and `VN_logs`). Your
   database and logs will be maintained across updates as they are stored in these Docker-managed volumes.

### Option 3: Self-Build

1. Download the latest [release](https://github.com/Fuyucch1/Vinted-Notifications/releases/latest)
2. Back up your `vinted_notifications.db` file
3. Replace all files with the new ones
4. Restart the application

## ℹ️ Trademark notice

This is an unofficial tool. Vinted and the Vinted logo are trademarks of Vinted UAB; the "V" mark used for the app icon and
sidebar comes from the [Simple Icons](https://simpleicons.org) project (the glyph is CC0, the brand stays Vinted's).

## ℹ️ Trademark notice

This is an unofficial tool. Vinted and the Vinted logo are trademarks of Vinted UAB; the "V" mark used for the app icon and
sidebar comes from the [Simple Icons](https://simpleicons.org) project (the glyph is CC0, the brand stays Vinted's).

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📜 License

This project is licensed under the [GNU AFFERO GENERAL PUBLIC LICENSE](LICENSE).

## 🙏 Acknowledgements

- Thanks to [@herissondev](https://github.com/herissondev) for maintaining pyVinted, a core dependency of this project.
