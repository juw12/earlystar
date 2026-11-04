// EarlyStar desktop: a native window around the live site, so data is always current.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{Url, WebviewUrl, WebviewWindowBuilder};

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
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running EarlyStar");
}
