# Install the rlp standalone binary on Windows.
#
#   irm https://raw.githubusercontent.com/rdlucas2/rlp/main/install.ps1 | iex
#
# Environment:
#   RLP_VERSION        release tag to install, e.g. v0.1.0 (default: latest)
#   RLP_INSTALL_DIR    where to put rlp.exe (default: %LOCALAPPDATA%\rlp\bin)
#   RLP_DOWNLOAD_BASE  base URL of the release assets (default: GitHub Releases)
$ErrorActionPreference = 'Stop'

$repo = 'rdlucas2/rlp'
$version = if ($env:RLP_VERSION) { $env:RLP_VERSION } else { 'latest' }
$installDir = if ($env:RLP_INSTALL_DIR) { $env:RLP_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'rlp\bin' }

if ($env:RLP_DOWNLOAD_BASE) {
  $base = $env:RLP_DOWNLOAD_BASE
} elseif ($version -eq 'latest') {
  $base = "https://github.com/$repo/releases/latest/download"
} else {
  $base = "https://github.com/$repo/releases/download/$version"
}

$arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
if ($arch -ne [System.Runtime.InteropServices.Architecture]::X64) {
  throw "rlp: no Windows binary for $arch yet; install the Python package instead: pipx install rlp-cli"
}
$asset = 'rlp-windows-x86_64.exe'

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  Write-Host "rlp: downloading $asset ($version)"
  Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset" -OutFile (Join-Path $tmp $asset)
  Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS" -OutFile (Join-Path $tmp 'SHA256SUMS')

  $want = $null
  foreach ($line in Get-Content (Join-Path $tmp 'SHA256SUMS')) {
    $parts = $line -split '\s+', 2
    if ($parts.Count -eq 2 -and $parts[1].TrimStart('*') -eq $asset) { $want = $parts[0].ToLower() }
  }
  $got = (Get-FileHash -Algorithm SHA256 (Join-Path $tmp $asset)).Hash.ToLower()
  if (-not $want -or $want -ne $got) {
    throw "rlp: checksum mismatch for $asset (want $want, got $got)"
  }

  New-Item -ItemType Directory -Force -Path $installDir | Out-Null
  $target = Join-Path $installDir 'rlp.exe'
  Move-Item -Force (Join-Path $tmp $asset) $target
  Write-Host "rlp: installed $(& $target --version) to $target"
} finally {
  Remove-Item -Recurse -Force $tmp
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not ($userPath -split ';' | Where-Object { $_ -eq $installDir })) {
  [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ";$installDir").TrimStart(';'), 'User')
  Write-Host "rlp: added $installDir to your user PATH; open a new terminal to use rlp"
}
Write-Host 'rlp: the binary is not code-signed yet; if SmartScreen warns, choose More info > Run anyway'
