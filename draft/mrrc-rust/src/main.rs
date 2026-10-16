use mrrc_rust_draft::{app::AppRuntime, config::RuntimeConfig};

fn main() {
    let cfg = RuntimeConfig::from_env();
    let runtime = AppRuntime::from_config(cfg.clone()).expect("valid MRRC_RADIO_MODEL");
    println!(
        "MRRC Rust draft: model={} display={} web={}:{} serial={}@{}",
        cfg.radio_model,
        runtime.capabilities.display_name,
        cfg.web_host,
        cfg.web_port,
        cfg.serial_port,
        cfg.baud_rate
    );
}
