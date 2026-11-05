from pathlib import Path


ROOT = Path(__file__).parents[1]
PROVISIONING_TF = ROOT / "terraform/contabo/provisioning.tf"


def test_k3s_bootstrap_avoids_unpinned_pipe_to_shell():
    text = PROVISIONING_TF.read_text()
    assert "curl -sfL https://get.k3s.io |" not in text
    assert "K3S_INSTALL_SCRIPT_URL='https://raw.githubusercontent.com/k3s-io/k3s/v1.36.2+k3s1/install.sh'" in text
    assert "K3S_INSTALL_SCRIPT_SHA256='46177d4c99440b4c0311b67233823a8e8a2fc09693f6c89af1a7161e152fbfad'" in text
    assert "K3S_INSTALL_SCRIPT_SHA256  /tmp/k3s-install.sh" in text
    assert "sha256sum -c -" in text


def test_argocd_bootstrap_uses_pinned_verified_manifest():
    text = PROVISIONING_TF.read_text()
    assert "argoproj/argo-cd/stable/manifests/install.yaml" not in text
    assert "ARGOCD_MANIFEST_URL='https://raw.githubusercontent.com/argoproj/argo-cd/v2.13.3/manifests/install.yaml'" in text
    assert "ARGOCD_MANIFEST_SHA256='0940f3f92ee6b91141cefc5368b1379aed0aef898186879ea2ca5f2607ee2617'" in text
    assert "ARGOCD_MANIFEST_SHA256  /tmp/argocd-install.yaml" in text
    assert "sha256sum -c -" in text
    assert "kubectl apply -n argocd -f /tmp/argocd-install.yaml" in text
