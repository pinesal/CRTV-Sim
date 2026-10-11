param()

$ErrorActionPreference = 'Stop'

# Keep credentials in memory only. Never print request headers or response bodies.
if (-not ('RetroTVBreezeCredentialReader' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class RetroTVBreezeCredentialReader {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct Credential {
        public uint Flags;
        public uint Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }
    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredRead(string target, uint type, uint flags, out IntPtr pointer);
    [DllImport("advapi32.dll")]
    private static extern void CredFree(IntPtr pointer);

    public static bool TryRead(out string key, out int error) {
        key = null;
        error = 0;
        IntPtr pointer;
        if (!CredRead("RetroTV/BreezeBlue", 1, 0, out pointer)) {
            error = Marshal.GetLastWin32Error();
            return false;
        }
        try {
            Credential credential = (Credential)Marshal.PtrToStructure(pointer, typeof(Credential));
            byte[] bytes = new byte[credential.CredentialBlobSize];
            Marshal.Copy(credential.CredentialBlob, bytes, 0, bytes.Length);
            key = Encoding.UTF8.GetString(bytes);
            Array.Clear(bytes, 0, bytes.Length);
            return true;
        } finally {
            CredFree(pointer);
        }
    }
}
'@
}

function Test-BreezeEndpoint {
    param([string]$Name, [string]$Path, [hashtable]$Headers)
    try {
        $response = Invoke-WebRequest -Uri ('https://api.breeze.blue' + $Path) `
            -Headers $Headers -UseBasicParsing -TimeoutSec 30
        $content = ConvertFrom-Json -InputObject $response.Content
        if ($Name -eq 'Models') {
            Write-Output ("Models: HTTP {0}; {1} available" -f $response.StatusCode, @($content).Count)
        } else {
            Write-Output ("Voices: HTTP {0}; {1} on first page" -f $response.StatusCode, @($content.voices).Count)
        }
        return $true
    } catch {
        $exception = $_.Exception
        if ($exception.Response) {
            Write-Output ("{0}: HTTP {1}" -f $Name, [int]$exception.Response.StatusCode)
        } else {
            Write-Output ("{0}: {1}: {2}" -f $Name, $exception.GetType().Name, $exception.Message)
            while ($exception.InnerException) {
                $exception = $exception.InnerException
                Write-Output ("  {0}: {1}" -f $exception.GetType().Name, $exception.Message)
            }
        }
        return $false
    }
}

Write-Output 'CRTV Sim Breeze Blue diagnostic (read-only; no speech generation)'
Write-Output ('Computer: ' + $env:COMPUTERNAME)
Write-Output ('Windows user: ' + $env:USERNAME)
Write-Output ('UTC clock: ' + [DateTime]::UtcNow.ToString('o'))

$breezeKey = [Environment]::GetEnvironmentVariable('BREEZE_API_KEY', 'Process')
$keySource = 'process environment'
if ([string]::IsNullOrWhiteSpace($breezeKey)) {
    $breezeKey = [Environment]::GetEnvironmentVariable('BREEZE_API_KEY', 'User')
    $keySource = 'user environment'
}
if ([string]::IsNullOrWhiteSpace($breezeKey)) {
    $credentialError = 0
    $found = [RetroTVBreezeCredentialReader]::TryRead([ref]$breezeKey, [ref]$credentialError)
    $keySource = 'Windows Credential Manager'
    if (-not $found) {
        if ($credentialError -eq 1168) {
            Write-Output 'Key: not found for this Windows user. Run this on the NUC as the user running CRTV Sim.'
        } else {
            Write-Output ('Key: Credential Manager access failed; Windows error ' + $credentialError)
        }
        exit 1
    }
}
if ([string]::IsNullOrWhiteSpace($breezeKey)) {
    Write-Output 'Key: empty'
    exit 1
}
$breezeKey = $breezeKey.Trim()
Write-Output ('Key: detected in ' + $keySource)

# TLS 1.2 uses Windows certificate validation, with verification enabled.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$headers = @{ 'xi-api-key' = $breezeKey; 'user-agent' = 'CRTV-Sim-Breeze-Diagnostic' }
try {
    $modelResult = @(Test-BreezeEndpoint -Name 'Models' -Path '/v1/models' -Headers $headers)
    $voiceResult = @(Test-BreezeEndpoint -Name 'Voices' -Path '/v1/voices?page=1&page_size=1' -Headers $headers)
    $modelResult | Where-Object { $_ -is [string] } | Write-Output
    $voiceResult | Where-Object { $_ -is [string] } | Write-Output
    if ($modelResult[-1] -and $voiceResult[-1]) {
        Write-Output 'Result: the saved key and Windows HTTPS connection work. Investigate the CRTV Sim SDK connection.'
    } else {
        Write-Output 'Result: see the endpoint error above. HTTP 401 means authentication failed; 403 means access denied; 5xx means a service error.'
    }
} finally {
    $headers.Clear()
    $breezeKey = $null
}
