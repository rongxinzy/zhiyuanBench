[CmdletBinding()]
param(
  [string]$ConfigFile = "",
  [switch]$NoBrowser,
  [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

if (-not $ConfigFile) {
  $ConfigFile = Join-Path $PSScriptRoot ".zhiyuan-bench.ui.local.json"
}
$ConfigFile = [System.IO.Path]::GetFullPath($ConfigFile)
if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
  throw "Missing local UI config: $ConfigFile. Copy .zhiyuan-bench.ui.example.json once and edit the local copy."
}

$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigFile | ConvertFrom-Json
foreach ($name in @("python", "repo", "workspace", "recordsRoot", "host", "port", "environment")) {
  if ($null -eq $config.PSObject.Properties[$name]) {
    throw "Local UI config is missing '$name': $ConfigFile"
  }
}

$python = [Environment]::ExpandEnvironmentVariables([string]$config.python)
$repo = [Environment]::ExpandEnvironmentVariables([string]$config.repo)
$workspace = [Environment]::ExpandEnvironmentVariables([string]$config.workspace)
$recordsRoot = [Environment]::ExpandEnvironmentVariables([string]$config.recordsRoot)
$hostName = [string]$config.host
$port = [int]$config.port
$sourceRoot = Join-Path $PSScriptRoot "src"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
  throw "Configured Python does not exist: $python"
}
foreach ($path in @($repo, $workspace, $sourceRoot)) {
  if (-not (Test-Path -LiteralPath $path -PathType Container)) {
    throw "Configured directory does not exist: $path"
  }
}
if ($port -le 0 -or $port -gt 65535) {
  throw "Configured port must be between 1 and 65535: $port"
}

$environment = @{}
foreach ($property in $config.environment.PSObject.Properties) {
  $environment[$property.Name] = [Environment]::ExpandEnvironmentVariables(
    [string]$property.Value
  )
}
foreach ($name in @("ZHIYUAN_MODEL_BASE_URL", "ZHIYUAN_MODEL_ID")) {
  if (-not $environment[$name]) {
    throw "Local UI config requires environment.$name"
  }
}
$environment["PYTHONPATH"] = $sourceRoot
$environment["PYTHONUTF8"] = "1"
if (-not $environment["DOCKER_CONFIG"]) {
  $environment["DOCKER_CONFIG"] = Join-Path $workspace ".zhiyuan-bench/docker-config"
}
New-Item -ItemType Directory -Force -Path $environment["DOCKER_CONFIG"] | Out-Null

$existingListener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
if ($existingListener) {
  $pids = @($existingListener | ForEach-Object OwningProcess | Sort-Object -Unique) -join ", "
  throw "Port $port is already in use by PID $pids"
}

$previousEnvironment = @{}
try {
  foreach ($entry in $environment.GetEnumerator()) {
    $previousEnvironment[$entry.Key] = [Environment]::GetEnvironmentVariable(
      $entry.Key,
      "Process"
    )
    [Environment]::SetEnvironmentVariable(
      $entry.Key,
      [string]$entry.Value,
      "Process"
    )
  }

  & $python -c "import starlette, uvicorn, zhiyuan_bench.web"
  if ($LASTEXITCODE -ne 0) {
    throw "Configured Python is missing zhiyuan-bench Web dependencies"
  }

  $runtimeErrors = [System.Collections.Generic.List[string]]::new()
  $modelUrl = $environment["ZHIYUAN_MODEL_BASE_URL"].TrimEnd("/") + "/models"
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $modelUrl -TimeoutSec 10 | Out-Null
  } catch {
    $runtimeErrors.Add("model API is unreachable")
  }

  if ($environment["DOCKER_HOST"]) {
    $nativeErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & docker -H $environment["DOCKER_HOST"] info --format "{{.ServerVersion}}" 2>$null | Out-Null
    $dockerExitCode = $LASTEXITCODE
    $ErrorActionPreference = $nativeErrorPreference
    if ($dockerExitCode -ne 0) {
      $runtimeErrors.Add("Docker daemon is unreachable")
    }
  }

  if ($runtimeErrors.Count -eq 0) {
    Write-Host "Runtime ready: $($environment['ZHIYUAN_MODEL_ID']) / $($environment['DOCKER_HOST'])"
  } elseif ($CheckOnly) {
    throw "Runtime check failed: $($runtimeErrors -join '; ')"
  } else {
    Write-Warning "Runtime is unavailable ($($runtimeErrors -join '; ')). The UI will start with evaluation disabled."
  }
  Write-Host "Records: $recordsRoot"
  if ($CheckOnly) {
    Write-Host "One-click UI configuration is ready."
    return
  }

  $arguments = [System.Collections.Generic.List[string]]@(
    "-m", "zhiyuan_bench", "ui",
    "--repo", $repo,
    "--workspace", $workspace,
    "--records-root", $recordsRoot,
    "--host", $hostName,
    "--port", [string]$port
  )
  if (-not $NoBrowser) {
    $arguments.Add("--open-browser")
  }
  $url = "http://${hostName}:$port/"
  Write-Host "Zhiyuan Bench UI: $url"
  Write-Host "Press Ctrl+C to stop."
  Push-Location $workspace
  try {
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
      throw "UI server exited with code $LASTEXITCODE"
    }
  } finally {
    Pop-Location
  }
} finally {
  foreach ($entry in $previousEnvironment.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable(
      $entry.Key,
      $entry.Value,
      "Process"
    )
  }
}
