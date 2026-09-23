use serde::Serialize;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[derive(Clone, Serialize)]
pub struct ServiceStatus {
    pub status: String,
    pub base_url: String,
    pub managed: bool,
    pub error: Option<String>,
}

struct Inner {
    status: ServiceStatus,
    child: Option<Child>,
    owner: String,
    port: u16,
    closing: bool,
}

#[derive(Clone)]
pub struct LocalService(Arc<Mutex<Inner>>);

// Small local HTTP control requests only. The frontend uses its normal HTTP client
// for application data; no shell commands or process-name based termination.
fn request(port: u16, method: &str, path: &str, owner: Option<&str>) -> Option<String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(300)).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(1))).ok()?;
    stream
        .set_write_timeout(Some(Duration::from_secs(1)))
        .ok()?;
    let owner_header = owner
        .map(|value| format!("X-Mybot-Owner: {value}\r\n"))
        .unwrap_or_default();
    let message = format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nX-Mybot-Client: mybot-desktop\r\n{owner_header}Content-Length: 0\r\n\r\n");
    stream.write_all(message.as_bytes()).ok()?;
    let mut bytes = Vec::new();
    stream.take(65536).read_to_end(&mut bytes).ok()?;
    String::from_utf8(bytes).ok()
}

fn is_mybot(port: u16) -> bool {
    request(port, "GET", "/api/health", None).is_some_and(|response| {
        let Some((headers, body)) = response.split_once("\r\n\r\n") else {
            return false;
        };
        if !headers.starts_with("HTTP/1.1 200 ") {
            return false;
        }
        serde_json::from_str::<serde_json::Value>(body)
            .ok()
            .is_some_and(|value| value["service"] == "mybot" && value["status"] == "ok")
    })
}

fn project_root() -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(path) = std::env::var_os("MYBOT_PROJECT_ROOT") {
        candidates.push(PathBuf::from(path));
    }
    if let Ok(current) = std::env::current_dir() {
        candidates.extend(current.ancestors().map(Path::to_path_buf));
    }
    if let Ok(executable) = std::env::current_exe() {
        candidates.extend(executable.ancestors().skip(1).map(Path::to_path_buf));
    }
    candidates.push(Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."));
    candidates
        .into_iter()
        .find(|path| path.join("server/__main__.py").is_file())
}

fn port_for(root: Option<&Path>) -> u16 {
    let configured = std::env::var("MYBOT_API_PORT").ok().or_else(|| {
        let content = fs::read_to_string(root?.join("config/.env")).ok()?;
        content.lines().find_map(|line| {
            let (key, value) = line.split_once('=')?;
            (key.trim() == "MYBOT_API_PORT")
                .then(|| value.trim().trim_matches(['\'', '"']).to_owned())
        })
    });
    configured
        .and_then(|value| value.parse().ok())
        .filter(|port| *port > 0)
        .unwrap_or(8765)
}

fn interpreter(root: &Path) -> std::ffi::OsString {
    if let Some(python) = std::env::var_os("MYBOT_PYTHON") {
        return python;
    }
    for folder in [".venv", "venv"] {
        let path = root.join(folder).join(if cfg!(windows) {
            "Scripts/python.exe"
        } else {
            "bin/python"
        });
        if path.is_file() {
            return path.into_os_string();
        }
    }
    if cfg!(windows) {
        "python".into()
    } else {
        "python3".into()
    }
}

impl Default for LocalService {
    fn default() -> Self {
        let root = project_root();
        let port = port_for(root.as_deref());
        Self(Arc::new(Mutex::new(Inner {
            status: ServiceStatus {
                status: "idle".into(),
                base_url: format!("http://127.0.0.1:{port}"),
                managed: false,
                error: None,
            },
            child: None,
            owner: String::new(),
            port,
            closing: false,
        })))
    }
}

impl LocalService {
    pub fn status(&self) -> ServiceStatus {
        let mut state = self.0.lock().unwrap();
        if state.status.status == "connected"
            && state
                .child
                .as_mut()
                .is_some_and(|child| child.try_wait().ok().flatten().is_some())
        {
            state.child = None;
            state.status.status = "failed".into();
            state.status.error = Some("服务已退出，请检查服务日志。".into());
        }
        let probe = state.status.status == "connected" && state.child.is_none();
        let port = state.port;
        drop(state);
        if probe && !is_mybot(port) {
            let mut state = self.0.lock().unwrap();
            if state.status.status == "connected" && state.child.is_none() && !state.closing {
                state.status.status = "failed".into();
                state.status.error = Some("已有服务已断开，可重试连接或启动。".into());
            }
        }
        let state = self.0.lock().unwrap();
        state.status.clone()
    }

    pub fn start(&self) {
        {
            let mut state = self.0.lock().unwrap();
            if state.closing || matches!(state.status.status.as_str(), "starting" | "connected") {
                return;
            }
            state.status.status = "starting".into();
            state.status.error = None;
            state.status.managed = false;
        }
        let service = self.clone();
        std::thread::spawn(move || service.launch());
    }

    fn launch(&self) {
        let port = self.0.lock().unwrap().port;
        if is_mybot(port) {
            let mut state = self.0.lock().unwrap();
            if !state.closing {
                state.status.status = "connected".into();
            }
            return;
        }
        // An occupied port belonging to another application must not be taken over.
        if TcpStream::connect_timeout(
            &SocketAddr::from(([127, 0, 0, 1], port)),
            Duration::from_millis(300),
        )
        .is_ok()
        {
            self.fail("本机服务端口已被其他程序占用。", false);
            return;
        }
        let Some(root) = project_root() else {
            self.fail("未找到 Python 服务目录，请设置 MYBOT_PROJECT_ROOT。", false);
            return;
        };
        let result = (|| -> std::io::Result<Child> {
            let log_dir = root.join("data/log");
            fs::create_dir_all(&log_dir)?;
            let log = OpenOptions::new()
                .create(true)
                .append(true)
                .open(log_dir.join("api-service-console.log"))?;
            let mut state = self.0.lock().unwrap();
            if state.closing {
                return Err(std::io::ErrorKind::Interrupted.into());
            }
            state.owner = uuid::Uuid::new_v4().to_string();
            let mut command = Command::new(interpreter(&root));
            command
                .args(["-u", "-m", "server", "--port", &port.to_string()])
                .current_dir(&root)
                .env("MYBOT_SERVICE_OWNER_TOKEN", &state.owner)
                .env("PYTHONIOENCODING", "utf-8")
                .stdin(Stdio::null())
                .stdout(Stdio::from(log.try_clone()?))
                .stderr(Stdio::from(log));
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                command.creation_flags(0x08000000); // CREATE_NO_WINDOW
            }
            command.spawn()
        })();
        match result {
            Ok(child) => {
                let mut state = self.0.lock().unwrap();
                state.child = Some(child);
                state.status.managed = true;
                if state.closing {
                    drop(state);
                    self.stop();
                    return;
                }
            }
            Err(_) => {
                self.fail(
                    "服务启动失败，请检查 Python 环境及 data/log/api-service-console.log。",
                    false,
                );
                return;
            }
        }
        let deadline = Instant::now() + Duration::from_secs(30);
        while Instant::now() < deadline {
            {
                let mut state = self.0.lock().unwrap();
                if state.closing {
                    return;
                }
                if state
                    .child
                    .as_mut()
                    .is_some_and(|child| child.try_wait().ok().flatten().is_some())
                {
                    drop(state);
                    self.fail(
                        "服务已退出，请检查 data/log/api-service-console.log。",
                        true,
                    );
                    return;
                }
            }
            if is_mybot(port) {
                let mut state = self.0.lock().unwrap();
                if !state.closing {
                    state.status.status = "connected".into();
                }
                return;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        self.fail("服务未能及时就绪，请检查数据库连接与服务日志。", true);
    }

    fn fail(&self, message: &str, clean_child: bool) {
        let mut state = self.0.lock().unwrap();
        if clean_child {
            if let Some(mut child) = state.child.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
        if !state.closing {
            state.status.status = "failed".into();
            state.status.error = Some(message.into());
        }
    }

    pub fn stop(&self) {
        let (child, owner, port) = {
            let mut state = self.0.lock().unwrap();
            state.closing = true;
            (state.child.take(), state.owner.clone(), state.port)
        };
        if let Some(mut child) = child {
            let _ = request(port, "POST", "/api/service/shutdown", Some(&owner));
            let deadline = Instant::now() + Duration::from_secs(10);
            while Instant::now() < deadline {
                if child.try_wait().ok().flatten().is_some() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

#[tauri::command]
pub async fn local_service_status(
    state: tauri::State<'_, LocalService>,
) -> Result<ServiceStatus, String> {
    let service = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || service.status())
        .await
        .map_err(|_| "读取服务状态失败。".into())
}

#[tauri::command]
pub fn start_local_service(state: tauri::State<'_, LocalService>) {
    state.start();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn health_server(body: &str) -> (u16, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let body = body.to_owned();
        let task = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            while !request.ends_with(b"\r\n\r\n") {
                let mut byte = [0; 1];
                stream.read_exact(&mut byte).unwrap();
                request.push(byte[0]);
            }
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        });
        (port, task)
    }

    #[test]
    fn only_reuses_mybot_health_response() {
        let (port, task) = health_server(r#"{"status":"ok","service":"mybot"}"#);
        assert!(is_mybot(port));
        task.join().unwrap();
        let (port, task) = health_server(r#"{"status":"ok","service":"unrelated"}"#);
        assert!(!is_mybot(port));
        task.join().unwrap();
    }

    #[test]
    fn manual_service_has_no_child_to_terminate() {
        let service = LocalService::default();
        service.stop();
        assert!(service.0.lock().unwrap().child.is_none());
    }

    #[test]
    fn reused_service_disconnect_is_reported() {
        let (port, task) = health_server(r#"{"status":"ok","service":"mybot"}"#);
        let service = LocalService::default();
        {
            let mut state = service.0.lock().unwrap();
            state.port = port;
            state.status.status = "connected".into();
        }
        assert_eq!(service.status().status, "connected");
        task.join().unwrap();
        assert_eq!(service.status().status, "failed");
        assert!(!service.status().managed);
    }
}
