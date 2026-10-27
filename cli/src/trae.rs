//! Explicit single-credential Trae relay support. No registry admission is implied.
use anyhow::{bail, Result};
use serde::Serialize;

use crate::{config::ProviderKeys, network::Network, types::SourceProduct};

pub const MODEL: &str = "deepseek-v4.1-flash";
pub const ENV_URL: &str = "TRAE_CPA_BASE_URL";
pub const ENV_KEY: &str = "TRAE_CPA_API_KEY";

/// Memory-only credential: omitted by `ProviderKeys` serde and redacted in Debug.
#[derive(Clone)]
pub struct RuntimeKey(String);

impl From<String> for RuntimeKey {
    fn from(value: String) -> Self {
        Self(value)
    }
}
impl std::ops::Deref for RuntimeKey {
    type Target = str;
    fn deref(&self) -> &str {
        &self.0
    }
}
impl std::fmt::Debug for RuntimeKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[redacted]")
    }
}

/// Apply a parent process's secret injection without persisting the key.
pub fn apply_runtime(keys: &mut ProviderKeys, url: Option<String>, key: Option<String>) {
    if let Some(url) = url {
        keys.trae_cpa_base_url = Some(url);
    }
    keys.trae = key.map(RuntimeKey::from);
}

/// Strictly match the container's HTTPS host[:port]/v1 contract. Errors never echo URLs.
///
/// # Errors
/// Rejects endpoints outside the container's HTTPS URL contract.
pub fn validate_endpoint(value: &str) -> Result<()> {
    let invalid = || {
        anyhow::anyhow!("Trae endpoint must be https://DNS-host[:port]/v1 without userinfo, query, fragment or whitespace")
    };
    if !value.starts_with("https://") || value.chars().any(|c| c.is_whitespace() || c.is_control())
    {
        return Err(invalid());
    }
    let url = reqwest::Url::parse(value).map_err(|_| invalid())?;
    let host = url.host_str().ok_or_else(invalid)?;
    let labels_valid = host.split('.').all(|label| {
        !label.is_empty()
            && label
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-')
            && label
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphanumeric)
            && label
                .as_bytes()
                .last()
                .is_some_and(u8::is_ascii_alphanumeric)
    });
    if !labels_valid
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.port() == Some(0)
        || !matches!(url.path(), "/v1" | "/v1/")
    {
        return Err(invalid());
    }
    Ok(())
}

#[must_use]
pub fn enabled(keys: &ProviderKeys) -> bool {
    keys.trae_cpa_base_url.is_some() || keys.trae.is_some()
}

/// Validate a complete, single-credential relay configuration.
///
/// # Errors
/// Rejects missing fields, unsafe endpoints, or malformed credentials.
pub fn validate_keys(keys: &ProviderKeys) -> Result<()> {
    if !enabled(keys) {
        return Ok(());
    }
    let Some(url) = keys.trae_cpa_base_url.as_deref() else {
        bail!("Trae requires {ENV_URL} or --trae-cpa-base-url");
    };
    validate_endpoint(url)?;
    let Some(key) = keys.trae.as_deref() else {
        bail!(
            "Trae requires {ENV_KEY} injected into this process; it is never read from config.json"
        );
    };
    if key.is_empty()
        || key
            .chars()
            .any(|c| c == ';' || c.is_whitespace() || c.is_control())
    {
        bail!(
            "{ENV_KEY} must be one non-empty credential without whitespace or control characters"
        );
    }
    Ok(())
}

/// Kept separate so all ordinary deployments retain the release's exact measurement.
#[must_use]
pub fn compose_template() -> String {
    crate::deploy::COMPOSE_TEMPLATE.replacen(
        "      - GM_NODE_SECRET",
        "      - TRAE_CPA_BASE_URL\n      - TRAE_CPA_API_KEY\n      - GM_NODE_SECRET",
        1,
    )
}

/// Require an exact source/buyer mapping. A buyer catalog entry alone is insufficient.
///
/// # Errors
/// Rejects any source or buyer mapping outside the selected `DeepSeek` route.
pub fn validate_route(route: &SourceProduct) -> Result<()> {
    if route.provider != "trae"
        || route.model != MODEL
        || route.buyer_provider != "deepseek"
        || route.buyer_model != MODEL
    {
        bail!("Trae requires an explicit trae/{MODEL} -> deepseek/{MODEL} registry route");
    }
    Ok(())
}

#[derive(Serialize)]
pub struct Offer {
    pub provider: &'static str,
    pub model: &'static str,
    pub discount_bp: u32,
}

#[derive(Serialize)]
pub struct Plan {
    pub candidate_only: bool,
    pub image_ref: String,
    pub compose_hash: String,
    pub os_image_hash: &'static str,
    pub required_env_names: [&'static str; 3],
    pub required_buyer_product: &'static str,
    pub offer: Offer,
}

/// Offline review only: no key, registry write, image publication or Phala spending.
///
/// # Errors
/// Rejects unpinned images, invalid discounts, or unrenderable compose templates.
pub fn plan(image_ref: &str, network: Network, discount_bp: u32) -> Result<Plan> {
    let Some((repo, digest)) = image_ref.rsplit_once("@sha256:") else {
        bail!("Trae plan requires a digest-pinned image reference");
    };
    if repo.is_empty()
        || repo.contains(['@', '?', '#'])
        || repo.contains("://")
        || repo.chars().any(char::is_whitespace)
        || digest.len() != 64
        || !digest
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        bail!("Trae plan requires a digest-pinned image reference");
    }
    if discount_bp > 9990 {
        bail!("discount exceeds the registry's 99.90% maximum");
    }
    Ok(Plan {
        candidate_only: true,
        image_ref: image_ref.into(),
        compose_hash: crate::compose_hash::compute_compose_hash_for_trae(image_ref, network)?,
        os_image_hash: crate::compose_hash::PINNED_OS_IMAGE_HASH,
        required_env_names: [ENV_URL, ENV_KEY, "GM_NODE_SECRET"],
        required_buyer_product: "deepseek/deepseek-v4.1-flash",
        offer: Offer {
            provider: "trae",
            model: MODEL,
            discount_bp,
        },
    })
}
