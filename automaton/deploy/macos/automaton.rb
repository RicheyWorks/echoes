# Homebrew formula for automaton. Drop into your own tap, e.g.:
#   brew tap your-name/automaton https://github.com/your-name/homebrew-automaton
#   brew install your-name/automaton/automaton
#
# Resource SHA-256s are placeholders - regenerate with
# `brew create --python <url>` when you fork this.

class Automaton < Formula
  include Language::Python::Virtualenv

  desc "Strongly consistent personal automation platform"
  homepage "https://github.com/your-name/automaton"
  url "https://github.com/your-name/automaton/archive/v0.2.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  depends_on "python@3.11"

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/source/p/pyyaml/PyYAML-6.0.2.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.28.1.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "croniter" do
    url "https://files.pythonhosted.org/packages/source/c/croniter/croniter-6.2.2.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "yoyo-migrations" do
    url "https://files.pythonhosted.org/packages/source/y/yoyo-migrations/yoyo-migrations-9.0.0.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "keyring" do
    url "https://files.pythonhosted.org/packages/source/k/keyring/keyring-25.0.0.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "apprise" do
    url "https://files.pythonhosted.org/packages/source/a/apprise/apprise-1.10.0.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  # Transitive deps (anyio, sqlparse, etc.) get pulled by `virtualenv_install_with_resources`.

  def install
    virtualenv_install_with_resources

    # Ship the launchd plists + examples + templates so install.sh can
    # find them without re-cloning the repo.
    libexec.install "deploy/macos"
    libexec.install "examples"
    libexec.install "templates"
  end

  def caveats
    <<~EOS
      To finish the install, run the launchd installer for your user:
        bash "#{libexec}/macos/install.sh" "#{HOMEBREW_PREFIX}"

      That copies the plists into ~/Library/LaunchAgents and starts
      worker / scheduler / ui under launchd. Edit
      ~/Library/Application\\ Support/automaton/automaton.env and re-run
      the same command to refresh.

      Logs live at ~/Library/Logs/automaton/.
      Hit the UI at http://127.0.0.1:8080/.
    EOS
  end

  test do
    # Smoke: the binary runs and reports its version-ish summary.
    assert_match "automaton", shell_output("#{bin}/automaton --help")
  end
end
