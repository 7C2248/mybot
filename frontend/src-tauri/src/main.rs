#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod service;
use tauri::Manager;

// Move and resize the borderless window together: separate IPC calls expose an
// intermediate frame in which the input bar jumps to the old window's top.
#[tauri::command]
fn set_compact_bounds(
    window: tauri::WebviewWindow,
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    minimum_height: f64,
) -> Result<(), String> {
    if window.label() != "main" || window.is_decorated().map_err(|e| e.to_string())? {
        return Err("Compact bounds require the borderless main window".into());
    }
    if width == 0
        || height == 0
        || width > i32::MAX as u32
        || height > i32::MAX as u32
        || !minimum_height.is_finite()
        || minimum_height <= 0.0
    {
        return Err("Invalid compact window dimensions".into());
    }
    let shrinking = height < window.inner_size().map_err(|e| e.to_string())?.height;
    let minimum = tauri::LogicalSize::new(336.0, minimum_height);
    // Lower constraints before shrinking; raise them after expansion so the OS
    // does not perform a separate resize at the previous top-left position.
    if shrinking {
        window
            .set_min_size(Some(minimum))
            .map_err(|e| e.to_string())?;
    }
    #[cfg(windows)]
    {
        use windows::Win32::UI::WindowsAndMessaging::{
            SetWindowPos, SWP_NOACTIVATE, SWP_NOCOPYBITS, SWP_NOOWNERZORDER, SWP_NOZORDER,
        };
        let hwnd = window.hwnd().map_err(|e| e.to_string())?;
        // The handle belongs to this live Tauri window; only its geometry changes.
        unsafe {
            SetWindowPos(
                hwnd,
                None,
                x,
                y,
                width as i32,
                height as i32,
                SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER | SWP_NOCOPYBITS,
            )
        }
        .map_err(|e| e.to_string())?;
    }
    #[cfg(not(windows))]
    {
        window
            .set_size(tauri::PhysicalSize::new(width, height))
            .map_err(|e| e.to_string())?;
        window
            .set_position(tauri::PhysicalPosition::new(x, y))
            .map_err(|e| e.to_string())?;
    }
    if !shrinking {
        window
            .set_min_size(Some(minimum))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn main() {
    let app = tauri::Builder::default()
        .manage(service::LocalService::default())
        .setup(|app| {
            app.state::<service::LocalService>().start();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            set_compact_bounds,
            service::local_service_status,
            service::start_local_service
        ])
        .build(tauri::generate_context!())
        .expect("mybot desktop failed to start");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            handle.state::<service::LocalService>().stop();
        }
    });
}
