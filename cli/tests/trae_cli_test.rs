//! Trae's operator path is opt-in and must not claim registry admission.
#![expect(
    clippy::unwrap_used,
    reason = "test assertions intentionally panic on unexpected values"
)]
use gm_miner_cli::{config::ProviderKeys, deploy, network::Network, slots, trae, types::Provider};

#[test]
fn trae_runtime_secret_is_not_a_persisted_key_or_debug_value() {
    let mut keys = ProviderKeys::default();
    trae::apply_runtime(
        &mut keys,
        Some("https://relay.example.test/v1".into()),
        Some("private-test-credential".into()),
    );
    assert!(keys.any_set());
    keys.validate_upstreams().unwrap();
    let json = serde_json::to_string(&keys).unwrap();
    assert!(!json.contains("private-test-credential"));
    assert!(!format!("{keys:?}").contains("private-test-credential"));
    let restored: ProviderKeys = serde_json::from_str(&json).unwrap();
    assert!(restored.trae.is_none());
    assert!(restored.validate_upstreams().is_err());
    assert!(
        !slots::provider_slots_for_keys(&keys, "test-node-secret-00000000")
            .unwrap()
            .contains_key("trae")
    );
}

#[test]
fn trae_endpoint_and_single_credential_validation_do_not_echo_rejected_secrets() {
    for url in [
        "http://relay.test/v1",
        "https://user:password@relay.test/v1",
        "https://relay.test/v1?key=password",
        "https://relay.test/v1#password",
        "https://relay.test:0/v1",
        "https://relay.test/api",
        "https://bad..test/v1",
    ] {
        let error = trae::validate_endpoint(url).unwrap_err().to_string();
        assert!(!error.contains("password"));
    }
    for key in [
        "",
        "two;credentials",
        "secret\nsecond-line",
        "secret with space",
    ] {
        let mut keys = ProviderKeys::default();
        trae::apply_runtime(
            &mut keys,
            Some("https://relay.test/v1".into()),
            Some(key.into()),
        );
        assert!(keys.validate_upstreams().is_err());
    }
    assert!(trae::validate_endpoint("https://relay.test:8443/v1/").is_ok());
}

#[test]
fn trae_deploy_envs_and_measurement_share_the_same_opt_in_template() {
    let image = format!("ghcr.io/example/miner@sha256:{}", "a".repeat(64));
    let mut keys = ProviderKeys::default();
    trae::apply_runtime(
        &mut keys,
        Some("https://relay.test/v1".into()),
        Some("fixture-credential".into()),
    );
    let env = deploy::render_env_file(&keys, "test-node-secret-00000000", None);
    assert!(env.contains("TRAE_CPA_BASE_URL=https://relay.test/v1\n"));
    assert!(env.contains("TRAE_CPA_API_KEY=fixture-credential\n"));
    let ordinary =
        gm_miner_cli::compose_hash::compute_compose_hash(&image, Network::Mainnet).unwrap();
    let plan = trae::plan(&image, Network::Mainnet, 1000).unwrap();
    assert_ne!(ordinary, plan.compose_hash);
    assert!(plan.candidate_only);
    assert_eq!(plan.offer.provider, "trae");
    assert_eq!(plan.offer.model, "deepseek-v4.1-flash");
    let template = trae::compose_template();
    assert!(template.contains("- TRAE_CPA_API_KEY"));
    assert!(!template.contains("fixture-credential"));
    let plain_env = deploy::render_env_file(&ProviderKeys::default(), "node", None);
    assert!(!plain_env.contains("TRAE_CPA"));
    assert_eq!("trae".parse::<Provider>().unwrap(), Provider::Trae);
}

#[test]
fn trae_never_substitutes_a_different_buyer_or_model() {
    let source = |buyer: &str, model: &str| {
        serde_json::from_value(serde_json::json!({
            "provider":"trae", "model":"deepseek-v4.1-flash", "buyer_provider":buyer,
            "buyer_model":model, "retail_price":{"dimensions":{"input_per_mtok_ndollars":140_000_000,"output_per_mtok_ndollars":280_000_000},"modifiers":{},"surcharges":{}},
            "capable_worker_count":0,"already_offered":false
        }))
        .unwrap()
    };
    assert!(trae::validate_route(&source("deepseek", "deepseek-v4.1-flash")).is_ok());
    assert!(trae::validate_route(&source("zai", "glm-5.3")).is_err());
    assert!(trae::plan("ghcr.io/example/miner:mutable", Network::Mainnet, 1000).is_err());
    assert!(trae::plan(
        &format!("ghcr.io/example/miner@sha256:{}", "a".repeat(64)),
        Network::Mainnet,
        10000
    )
    .is_err());
}

#[test]
fn offline_cli_plan_and_configuration_do_not_persist_or_print_the_runtime_key() {
    let temp = tempfile::tempdir().unwrap();
    let run = |args: &[&str]| {
        std::process::Command::new(env!("CARGO_BIN_EXE_gmcli"))
            .env("GMCLI_CONFIG_DIR", temp.path())
            .env("TRAE_CPA_API_KEY", "secret-cli-fixture-12345")
            .env_remove("TRAE_CPA_BASE_URL")
            .args(args)
            .output()
            .unwrap()
    };
    let image = format!("ghcr.io/example/miner@sha256:{}", "a".repeat(64));
    let out = run(&["--network", "mainnet", "trae-plan", "--image-ref", &image]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let plan: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(plan["candidate_only"], true);
    assert!(!temp.path().join("config.json").exists());
    let out = run(&[
        "set-api-keys",
        "--trae-cpa-base-url",
        "https://relay.test/v1",
    ]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let config = std::fs::read_to_string(temp.path().join("config.json")).unwrap();
    assert!(config.contains("https://relay.test/v1"));
    assert!(!config.contains("secret-cli-fixture"));
    assert!(!String::from_utf8_lossy(&out.stdout).contains("secret-cli-fixture"));
    assert!(!String::from_utf8_lossy(&out.stderr).contains("secret-cli-fixture"));
}
