# 仅发布功能；正式数据库和 .env 始终由服务器独立维护。
param(
    [switch]$Frontend,
    [switch]$Backend,
    [switch]$Db,
    [switch]$DryRun,
    [string]$Server = $env:ZGNB_DEPLOY_SERVER,
    [string]$SshKey = "$env:USERPROFILE\.ssh\zgnb_deploy_key",
    [string]$RemoteDir = "/www/zgnb",
    [string]$ServiceName = "zgnb-api"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Helper = Join-Path $PSScriptRoot "deploy_package.py"
$DoFrontend = $Frontend -or (-not $Backend)
$DoBackend = $Backend -or (-not $Frontend)

function Invoke-External {
    param([string]$Program, [string[]]$Arguments)
    $global:LASTEXITCODE = 0
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "步骤失败：$Program，退出码 $LASTEXITCODE；已停止后续发布。"
    }
}

try {
    if ($Db) {
        throw "DATA_IMPORT_DISABLED：发布禁止上传数据库。首次数据请用 export_database.py 导出后单独导入。"
    }
    if ($args.Count -gt 0) { throw "存在未知参数，已停止发布。" }
    if ($RemoteDir -notmatch '^/[A-Za-z0-9_./-]+$' -or $RemoteDir.Trim('/') -eq '' -or $RemoteDir.Split('/') -contains '..' -or $RemoteDir.Split('/') -contains '.') {
        throw "RemoteDir 必须是非根目录的安全绝对路径。"
    }
    if ($ServiceName -notmatch '^[A-Za-z0-9_.@-]+$') { throw "ServiceName 格式无效。" }
    if ($DryRun) {
        Write-Host "DRY_RUN：项目=$ProjectDir；目标=$Server`:$RemoteDir"
        Write-Host "前端=$DoFrontend；后端=$DoBackend；仅允许功能文件与静态股票池配置。"
        Write-Host "不执行构建、打包、联网或重启；不发布数据库、.env、密钥和代理配置。"
        exit 0
    }
    if ([string]::IsNullOrWhiteSpace($Server) -or $Server -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.@-]*$') {
        throw "请通过 -Server 或 ZGNB_DEPLOY_SERVER 指定 SSH 目标（如 deploy@example.com）。"
    }
    if (-not (Test-Path -LiteralPath $SshKey -PathType Leaf)) { throw "SSH 密钥文件不存在。" }
    if (-not (Test-Path -LiteralPath $Helper -PathType Leaf)) { throw "缺少发布包校验工具。" }

    $ReleaseId = [guid]::NewGuid().ToString('N')
    $LocalStage = Join-Path ([System.IO.Path]::GetTempPath()) "zgnb-release-$ReleaseId"
    New-Item -ItemType Directory -Path $LocalStage | Out-Null
    $Packages = @()
    if ($DoFrontend) {
        Push-Location (Join-Path $ProjectDir "frontend")
        try { Invoke-External -Program "npm.cmd" -Arguments @("run", "build") }
        finally { Pop-Location }
        $Packages += "frontend"
    }
    if ($DoBackend) { $Packages += "backend" }
    foreach ($Kind in $Packages) {
        $Source = $ProjectDir
        if ($Kind -eq "frontend") { $Source = Join-Path $ProjectDir "frontend\dist" }
        Invoke-External -Program "python" -Arguments @($Helper, "build", "--kind", $Kind, "--source", $Source, "--archive", (Join-Path $LocalStage "$Kind.zip"))
    }

    # 所有构建和打包成功后才接触远端，不关闭 SSH 主机密钥检查。
    $RemoteStage = "$RemoteDir/.deploy/$ReleaseId"
    $SshOptions = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-i", $SshKey)
    $RemotePython = "$RemoteDir/venv/bin/python"
    Invoke-External -Program "ssh" -Arguments ($SshOptions + @($Server, "set -e; test -d '$RemoteDir'; test -f '$RemoteDir/.env'; test -x '$RemotePython'; mkdir -p '$RemoteStage'"))
    Invoke-External -Program "scp" -Arguments ($SshOptions + @($Helper, "${Server}:$RemoteStage/deploy_package.py"))
    foreach ($Kind in $Packages) {
        Invoke-External -Program "scp" -Arguments ($SshOptions + @((Join-Path $LocalStage "$Kind.zip"), "${Server}:$RemoteStage/$Kind.zip"))
    }
    foreach ($Kind in $Packages) {
        Invoke-External -Program "ssh" -Arguments ($SshOptions + @($Server, "'$RemotePython' '$RemoteStage/deploy_package.py' apply --kind '$Kind' --archive '$RemoteStage/$Kind.zip' --destination '$RemoteDir'"))
    }
    if ($DoBackend) {
        Invoke-External -Program "ssh" -Arguments ($SshOptions + @($Server, "systemctl restart '$ServiceName'"))
        Start-Sleep -Seconds 3
    }
    Invoke-External -Program "ssh" -Arguments ($SshOptions + @($Server, "'$RemotePython' '$RemoteStage/deploy_package.py' health"))
    Write-Host "功能发布完成，数据库和环境配置未覆盖。"
    Write-Host "发布包保留位置：$LocalStage；服务器：$RemoteStage。"
    exit 0
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
