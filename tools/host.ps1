#Requires -Version 7.0
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function New-ReproContext {
    param([string]$Distro = 'Ubuntu-22.04', [string]$Directory)
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw 'WSL is required. Run these scripts in PowerShell on Windows.'
    }
    $directoryPath = (Resolve-Path -LiteralPath $Directory).Path
    # Convert the drive root first. Converting a full Windows path can select
    # Docker Desktop's temporary bind alias and resolve child mounts incorrectly.
    if ($directoryPath -match '^[A-Za-z]:\\') {
        $driveRoot = [System.IO.Path]::GetPathRoot($directoryPath)
        $linuxDrive = & wsl.exe -d $Distro --exec wslpath -a -u $driveRoot
        if ($LASTEXITCODE -ne 0 -or -not $linuxDrive) { throw 'Cannot resolve the Windows drive in WSL.' }
        $linuxRoot = ($linuxDrive -join "`n").Trim().TrimEnd('/') + '/' + $directoryPath.Substring($driveRoot.Length).Replace('\', '/')
    } else {
        $linuxRoot = & wsl.exe -d $Distro --exec wslpath -a -u ($directoryPath.Replace('\', '/'))
        if ($LASTEXITCODE -ne 0 -or -not $linuxRoot) { throw 'Cannot resolve the repository path in WSL.' }
        $linuxRoot = ($linuxRoot -join "`n").Trim()
    }
    if ($linuxRoot.StartsWith('/mnt/wsl/docker-desktop-bind-mounts/')) {
        throw 'WSL selected an unstable Docker Desktop bind alias. Use a checkout on a mounted Windows drive.'
    }
    $context = [pscustomobject]@{ Distro = $Distro; LinuxRoot = $linuxRoot; Directory = $directoryPath }
    $linuxHash = & wsl.exe -d $Distro --exec sha256sum "$linuxRoot/source-lock.json"
    $windowsHash = (Get-FileHash -LiteralPath (Join-Path $directoryPath 'source-lock.json') -Algorithm SHA256).Hash
    if ($LASTEXITCODE -ne 0 -or -not $linuxHash -or ($linuxHash -join '').Split(' ')[0] -ine $windowsHash) {
        throw 'The Windows and WSL checkout paths do not identify the same source-lock.json.'
    }
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $identity = [System.Text.Encoding]::UTF8.GetBytes($directoryPath.TrimEnd('\').ToLowerInvariant())
        $checkoutId = [System.BitConverter]::ToString($hasher.ComputeHash($identity)).Replace('-', '').ToLowerInvariant()
    } finally { $hasher.Dispose() }
    # Desktop can report another bind alias in inspect even when mountinfo is
    # correct. An explicit checkout identity protects stopped containers too.
    $context | Add-Member -NotePropertyName CheckoutId -NotePropertyValue $checkoutId
    & wsl.exe -d $Distro --exec test -f /usr/lib/wsl/lib/libd3d12.so
    if ($LASTEXITCODE -ne 0) { throw 'The selected WSL distro has no /usr/lib/wsl/lib/libd3d12.so.' }
    return $context
}

function Invoke-ReproDocker {
    param($Context, [string[]]$Arguments, [switch]$AllowFailure)
    & wsl.exe -d $Context.Distro --cd $Context.LinuxRoot --exec env "ROBOTWIN_CHECKOUT_ID=$($Context.CheckoutId)" docker @Arguments
    if (-not $AllowFailure -and $LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code $LASTEXITCODE."
    }
}

function Get-ReproSettings {
    param($Context, [switch]$Bootstrap)
    $arguments = @('compose', '-f', 'compose.yaml')
    if ($Bootstrap) { $arguments += @('-f', 'compose.bootstrap.yaml') }
    $arguments += @('config', '--format', 'json')
    $raw = Invoke-ReproDocker -Context $Context -Arguments $arguments
    $configuration = ($raw -join "`n") | ConvertFrom-Json
    $projectName = [string]$configuration.name
    if ([string]::IsNullOrWhiteSpace($projectName)) { throw 'Expected a resolved Compose project name.' }
    $service = $configuration.services.robotwin
    $workspaceMount = @($service.volumes | Where-Object target -EQ '/workspace')
    if ($workspaceMount.Count -ne 1) { throw 'Expected one /workspace bind mount.' }
    $checkoutId = Get-ReproLabel -Labels $service.labels -Name 'org.robotwin.repro.checkout'
    if ($checkoutId -ne $Context.CheckoutId) { throw 'Compose did not preserve the current checkout identity.' }
    return [pscustomobject]@{
        ProjectName = $projectName
        ContainerName = $service.container_name
        Image = $service.image
        WorkspaceSource = $workspaceMount[0].source
        CheckoutId = $checkoutId
        BuildJobs = $(if ($Bootstrap) { $service.environment.ROBOTWIN_BUILD_JOBS } else { $null })
        CudaJobs = $(if ($Bootstrap) { $service.environment.ROBOTWIN_CUDA_JOBS } else { $null })
    }
}

function Get-ReproContainer {
    param($Context, [string]$Name)
    $raw = Invoke-ReproDocker -Context $Context -Arguments @('inspect', $Name) -AllowFailure 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return @(($raw -join "`n") | ConvertFrom-Json)[0]
}

function Get-ReproImage {
    param($Context, [string]$Image)
    $raw = Invoke-ReproDocker -Context $Context -Arguments @('image', 'inspect', $Image) -AllowFailure 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return @(($raw -join "`n") | ConvertFrom-Json)[0]
}

function Get-ReproLabel {
    param($Labels, [string]$Name)
    if ($null -eq $Labels) { return $null }
    $property = $Labels.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return [string]$property.Value
}

function Assert-ReproContainer {
    param($Container, $Settings)
    if ($null -eq $Container) { return }
    if ((Get-ReproLabel -Labels $Container.Config.Labels -Name 'org.robotwin.repro.managed') -ne 'true') {
        throw 'An unrelated container uses this name. Choose another ROBOTWIN_CONTAINER_NAME in .env.'
    }
    $mounts = @($Container.Mounts | Where-Object Destination -EQ '/workspace')
    if ($mounts.Count -ne 1 -or $mounts[0].Type -ne 'bind' -or -not $Settings.CheckoutId -or
        (Get-ReproLabel -Labels $Container.Config.Labels -Name 'org.robotwin.repro.checkout') -ne $Settings.CheckoutId) {
        throw 'This container belongs to another checkout. Choose a different project and container name.'
    }
    if ($Container.State.Paused) { throw 'The container is paused, possibly during image saving. Wait for that operation.' }
}

function Assert-ReproProjectContainers {
    param($Context, $Settings)
    # Compose selects existing services by project/service labels, even when
    # container_name changed. Check every such container before any compose up.
    $containerIds = @(Invoke-ReproDocker -Context $Context -Arguments @(
        'ps', '--all', '--quiet',
        '--filter', "label=com.docker.compose.project=$($Settings.ProjectName)",
        '--filter', 'label=com.docker.compose.service=robotwin'
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    foreach ($containerId in $containerIds) {
        $container = Get-ReproContainer -Context $Context -Name $containerId.Trim()
        if ($null -eq $container) {
            throw 'A matching Compose service container could not be inspected. Retry after other Docker operations finish.'
        }
        Assert-ReproContainer -Container $container -Settings $Settings
    }
}

function Assert-ReproReady {
    param($Context, $Settings)
    $image = Get-ReproImage -Context $Context -Image $Settings.Image
    if ($null -eq $image -or (Get-ReproLabel -Labels $image.Config.Labels -Name 'org.robotwin.setup_complete') -ne 'true') {
        throw 'The configured local image is missing. Run setup.ps1 first; do not recreate a partial setup container.'
    }
    $workspace = Join-Path $Context.Directory 'workspace'
    foreach ($relative in @('RoboTwin/envs/_base_task.py', 'RoboTwin/assets/embodiments/aloha-agilex/curobo_left.yml')) {
        if (-not (Test-Path -LiteralPath (Join-Path $workspace $relative))) {
            throw 'The workspace checkout or robot assets are missing. Run setup.ps1 with a new container name.'
        }
    }
}
