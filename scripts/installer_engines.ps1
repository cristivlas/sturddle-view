# Run by installer.iss after install: registers this installer's engine exes
# (|-separated names in $Names) in the app's engines.json. Additive: entries
# are never removed, and older exes left in $EnginesDir are not re-added.
# Known paths get their cached engine info cleared so the app re-probes.
param(
    [Parameter(Mandatory)] [string] $EnginesDir,
    [Parameter(Mandatory)] [string] $Names,
    [Parameter(Mandatory)] [string] $RegistryPath
)
$ErrorActionPreference = 'Stop'
# PS 5.1 otherwise serializes some arrays as {"value": [...], "Count": n}.
Remove-TypeData System.Array -ErrorAction SilentlyContinue

$IdHexChars = 12
$JsonDepth = 100

# Assigning a property a hand-written entry lacks would throw.
function Set-Field($Object, [string] $Name, $Value) {
    $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}

$exes = @($Names.Split('|') | ForEach-Object { Get-Item -LiteralPath (Join-Path $EnginesDir $_) })

if (Test-Path -LiteralPath $RegistryPath) {
    $data = Get-Content -LiteralPath $RegistryPath -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RegistryPath) | Out-Null
    $data = [pscustomobject]@{}
}
# Not @($data.engines): a missing key would yield @($null), a null entry.
$engines = @(if ($data.engines) { $data.engines })
$firstId = $null

foreach ($exe in $exes) {
    # String -eq is case-insensitive, matching Windows paths.
    $entry = $engines | Where-Object { $_.path -eq $exe.FullName } | Select-Object -First 1
    if ($entry) {
        Set-Field $entry 'option_schema' ([pscustomobject]@{})
        Set-Field $entry 'uci_name' $null
    } else {
        $entry = [pscustomobject]@{
            id   = [guid]::NewGuid().ToString('N').Substring(0, $IdHexChars)
            name = $exe.BaseName
            path = $exe.FullName
        }
        $engines += $entry
    }
    if (-not $firstId) { $firstId = $entry.id }
}

Set-Field $data 'engines' $engines
if (-not ($engines | Where-Object { $_.id -eq $data.selected_id })) {
    Set-Field $data 'selected_id' $firstId
}

# The app's json.load rejects a BOM; write UTF-8 without one, atomically.
$tmp = "$RegistryPath.tmp"
$json = ConvertTo-Json -InputObject $data -Depth $JsonDepth
[IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding $false))
Move-Item -LiteralPath $tmp -Destination $RegistryPath -Force
