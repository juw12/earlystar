// EarlyStar desktop: a native window around the live site, so data is always current.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::webview::DownloadEvent;
use tauri::{Manager, Url, WebviewUrl, WebviewWindowBuilder};

const SITE: &str = "https://juw12.github.io/earlystar/";

// Links that would open a new tab are turned into normal navigations,
// which `on_navigation` below sends to the system browser.
const LINK_HANDLER: &str = r#"
document.addEventListener("click", (event) => {
  const link = event.target.closest && event.target.closest('a[target="_blank"]');
  if (link && link.href) {
    event.preventDefault();
    window.location.href = link.href;
  }
}, true);
window.open = (url) => {
  if (url) window.location.href = new URL(url, window.location.href).href;
  return null;
};
"#;

fn is_inside_app(url: &Url) -> bool {
    url.host_str() == Some("juw12.github.io") && url.path().starts_with("/earlystar")
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(SITE.parse().expect("valid site URL")))
                .title("EarlyStar")
                .inner_size(1360.0, 880.0)
                .min_inner_size(900.0, 600.0)
                .initialization_script(LINK_HANDLER)
                .on_navigation(|url| {
                    let is_web = matches!(url.scheme(), "http" | "https");
                    if is_web && !is_inside_app(url) {
                        let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
                        return false;
                    }
                    true
                })
                // CSV export etc.: save into the user's Downloads folder, then show the file.
                .on_download(|webview, event| {
                    match event {
                        DownloadEvent::Requested { destination, .. } => {
                            if let Ok(downloads) = webview.app_handle().path().download_dir() {
                                let name = destination
                                    .file_name()
                                    .map(|name| name.to_owned())
                                    .unwrap_or_else(|| "earlystar-export.csv".into());
                                *destination = downloads.join(name);
                            }
                        }
                        DownloadEvent::Finished { path: Some(path), success: true, .. } => {
                            let _ = tauri_plugin_opener::reveal_item_in_dir(path);
                        }
                        _ => {}
                    }
                    true
                })
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running EarlyStar");
}
