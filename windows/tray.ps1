param([Parameter(Mandatory = $true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$config = $null
try {
    $config = [IO.File]::ReadAllText($ConfigPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Web.Extensions
    $source = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'TrayApp.cs'), [Text.Encoding]::UTF8)
    Add-Type -TypeDefinition $source -ReferencedAssemblies System.dll, System.Core.dll, System.Windows.Forms, System.Drawing, System.Web.Extensions
    [ClaudeCostLens.TrayApp]::Run($config.PythonCommand, [string[]]$config.PythonArgs, $config.AppPath, $config.StatusPath)
} catch {
    if ($null -ne $config) {
        $status = @{ state = 'error'; message = $_.Exception.Message } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($config.StatusPath, $status, [Text.UTF8Encoding]::new($false))
    }
    exit 1
}
