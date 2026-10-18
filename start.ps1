#Requires -Version 7.0
param([switch]$Enter, [string]$Distro = 'Ubuntu-22.04')
. (Join-Path $PSScriptRoot 'tools/host.ps1')
$env:WSL_UTF8 = '1'
$context = New-ReproContext -Distro $Distro -Directory $PSScriptRoot
$settings = Get-ReproSettings -Context $context
Assert-ReproReady -Context $context -Settings $settings
$container = Get-ReproContainer -Context $context -Name $settings.ContainerName
Assert-ReproContainer -Container $container -Settings $settings
Assert-ReproProjectContainers -Context $context -Settings $settings
Invoke-ReproDocker -Context $context -Arguments @('compose', '-f', 'compose.yaml', 'up', '-d', '--pull', 'never', 'robotwin')
if ($Enter) {
    Invoke-ReproDocker -Context $context -Arguments @('exec', '-it', '-w', '/workspace/RoboTwin', $settings.ContainerName, 'bash', '--noprofile', '--norc')
}
