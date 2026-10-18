#Requires -Version 7.0
param([string]$Distro = 'Ubuntu-22.04')
. (Join-Path $PSScriptRoot 'tools/host.ps1')
$env:WSL_UTF8 = '1'
$context = New-ReproContext -Distro $Distro -Directory $PSScriptRoot
$settings = Get-ReproSettings -Context $context
$bootstrapSettings = Get-ReproSettings -Context $context -Bootstrap
$logDirectory = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$lockPath = Join-Path $logDirectory 'setup.lock'
try {
    $setupLock = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    throw 'Another setup process holds logs/setup.lock. Let it finish before starting again.'
}
try {
    $container = Get-ReproContainer -Context $context -Name $settings.ContainerName
    Assert-ReproContainer -Container $container -Settings $settings
    Assert-ReproProjectContainers -Context $context -Settings $settings
    if ($null -ne $container -and (Get-ReproLabel -Labels $container.Config.Labels -Name 'org.robotwin.setup_complete') -eq 'true') {
        Write-Host 'This checkout already has a configured container. Starting it without rebuilding.'
        & (Join-Path $PSScriptRoot 'start.ps1') -Distro $Distro
        return
    }
    if ($null -eq $container) {
        Invoke-ReproDocker -Context $context -Arguments @('compose', '-f', 'compose.yaml', '-f', 'compose.bootstrap.yaml', 'up', '-d', '--pull', 'missing', 'robotwin')
    } else {
        # Resume the existing partial container without recreating its filesystem.
        if ($container.Config.Image -ne $bootstrapSettings.Image) {
            throw 'The partial container uses another image. Choose a new container name instead of overwriting it.'
        }
        if (-not $container.State.Running) {
            Invoke-ReproDocker -Context $context -Arguments @('start', $settings.ContainerName)
        }
    }
    $logPath = Join-Path $logDirectory ('bootstrap-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Invoke-ReproDocker -Context $context -Arguments @(
        'exec', '-w', '/workspace',
        '-e', "ROBOTWIN_BUILD_JOBS=$($bootstrapSettings.BuildJobs)",
        '-e', "ROBOTWIN_CUDA_JOBS=$($bootstrapSettings.CudaJobs)",
        $settings.ContainerName, 'bash', '/workspace/bootstrap_robotwin.sh'
    ) 2>&1 | Tee-Object -FilePath $logPath
    # Commit only after the in-container checks and real task have succeeded.
    $runtimePath = '/opt/conda/envs/robotwin/bin:/opt/conda/bin:/usr/local/nvidia/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    Invoke-ReproDocker -Context $context -Arguments @(
        'commit',
        '--change', "ENV PATH=$runtimePath",
        '--change', 'ENV CONDA_PREFIX=/opt/conda/envs/robotwin',
        '--change', 'ENV CONDA_DEFAULT_ENV=robotwin',
        '--change', 'ENV ROBOTWIN_CAMERA_SHADER=default',
        '--change', 'LABEL org.robotwin.setup_complete=true',
        '--change', 'LABEL org.robotwin.repro.managed=true',
        $settings.ContainerName, $settings.Image
    )
    & (Join-Path $PSScriptRoot 'start.ps1') -Distro $Distro
    & (Join-Path $PSScriptRoot 'validate.ps1') -Distro $Distro
    Write-Host "Ready: $($settings.ContainerName), image $($settings.Image). Run ./start.ps1 -Enter."
} finally {
    $setupLock.Dispose()
}
