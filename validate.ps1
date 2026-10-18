#Requires -Version 7.0
param([switch]$PlayOnce, [string]$Distro = 'Ubuntu-22.04', [string]$ContainerName)
. (Join-Path $PSScriptRoot 'tools/host.ps1')
$env:WSL_UTF8 = '1'
$context = New-ReproContext -Distro $Distro -Directory $PSScriptRoot
$settings = Get-ReproSettings -Context $context
if (-not $ContainerName) { $ContainerName = $settings.ContainerName }
$container = Get-ReproContainer -Context $context -Name $ContainerName
if ($null -eq $container) { throw 'The validation container does not exist. Run setup.ps1 first.' }
Assert-ReproContainer -Container $container -Settings $settings
if (-not $container.State.Running) { throw 'The validation container is not running. Start it before validating.' }
$validation = @'
set -euo pipefail
python -c 'import sys; assert sys.version_info[:2] == (3, 10), sys.version'
python /workspace/check_base.py
python /workspace/check_sapien.py render
python /workspace/check_cuda_extensions.py
python -m pip check
'@
Invoke-ReproDocker -Context $context -Arguments @('exec', '-w', '/workspace', $ContainerName, 'bash', '--noprofile', '--norc', '-c', $validation)
if ($PlayOnce) {
    Invoke-ReproDocker -Context $context -Arguments @('exec', '-w', '/workspace', $ContainerName, 'python', '-u', '/workspace/check_robotwin_task.py', '--play-once')
}
