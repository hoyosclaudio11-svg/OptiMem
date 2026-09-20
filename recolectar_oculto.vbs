' OptiMem - lanza el recolector sin ventana y lo relanza si se muere.
'
' Por que un VBS y no un acceso directo pelado: un .lnk arranca el proceso y se
' desentiende. Si el recolector se cae a las 3 de la manana, la recoleccion se
' detiene en silencio y uno se entera dias despues mirando que no hay datos.
' Este bucle lo relanza con una espera de 60 s, asi una caida se paga con un
' minuto de hueco y no con la recoleccion entera.
'
' Ademas llama al pythonw.exe real y no al shim de pyenv: el shim crea DOS
' procesos y despues es un lio saber cual hay que matar.
'
' Uso:  wscript recolectar_oculto.vbs
'       (o un acceso directo a este archivo en la carpeta de Inicio)

Option Explicit

Dim fso, sh, dir, py, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
py  = BuscarPythonW()

' Con --ruta solo informa que interprete usaria y sale. Sirve para verificar
' la deteccion sin arrancar la recoleccion.
If WScript.Arguments.Named.Exists("ruta") Or WScript.Arguments.Unnamed.Count > 0 Then
    WScript.Echo "pythonw: " & py
    WScript.Quit 0
End If

If py = "" Then
    MsgBox "No encontre pythonw.exe." & vbCrLf & vbCrLf & _
           "Indica la ruta en la variable OPTIMEM_PYTHONW, o edita" & vbCrLf & _
           "la funcion BuscarPythonW() de este archivo.", 16, "OptiMem"
    WScript.Quit 1
End If

sh.CurrentDirectory = dir
cmd = """" & py & """ """ & dir & "\optimem.py"" recolectar"

' El log de verdad lo escribe el recolector en optimem.log, dentro del
' directorio de datos. Si algo impide arrancar ANTES de que el logger exista,
' queda en errores_criticos.log: pythonw no tiene consola y sin eso el fallo
' desapareceria sin dejar rastro.

Do
    sh.Run cmd, 0, True      ' 0 = sin ventana, True = esperar a que termine
    WScript.Sleep 60000      ' si termino, esperar antes de relanzar
Loop


Function BuscarPythonW()
    ' Devuelve un pythonw.exe que EXISTA Y ADEMAS TENGA las dependencias.
    '
    ' No alcanza con que el archivo exista. En esta misma maquina hay tres
    ' instalaciones y DOS estan rotas para este uso: una no tiene sklearn y la
    ' otra lo tiene pero con un numpy binariamente incompatible, asi que falla
    ' al importar. Si el lanzador eligiera cualquiera de esas, el recolector
    ' arrancaria y se moriria sin decir nada, porque pythonw no tiene consola.
    '
    ' Por eso: (1) se recuerda el interprete que ya se verifico, para no
    ' volver a adivinar cada vez; (2) se verifica ejecutando el import de
    ' verdad, no mirando si el archivo esta.
    Dim lista, i, consola, recordado

    recordado = LeerRecordado()
    If recordado <> "" Then
        BuscarPythonW = recordado
        Exit Function
    End If

    lista = ListaCandidatos()
    For i = 0 To UBound(lista)
        consola = lista(i)
        If fso.FileExists(consola) Then
            Dim gemelo
            gemelo = fso.BuildPath(fso.GetParentFolderName(consola), "pythonw.exe")
            If fso.FileExists(gemelo) And TieneDependencias(consola) Then
                Recordar gemelo
                BuscarPythonW = gemelo
                Exit Function
            End If
        End If
    Next

    BuscarPythonW = ""
End Function


Function RutaRecordado()
    RutaRecordado = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & _
                    "\OptiMem\interprete.txt"
End Function


Function LeerRecordado()
    LeerRecordado = ""
    Dim f, ruta
    ruta = RutaRecordado()
    If Not fso.FileExists(ruta) Then Exit Function

    On Error Resume Next
    Set f = fso.OpenTextFile(ruta, 1)
    If Err.Number <> 0 Then Exit Function
    Dim py, verif
    py = Trim(f.ReadLine())
    verif = Trim(f.ReadLine())    ' ruta del python.exe con el que se verifico
    f.Close
    On Error GoTo 0

    ' Se re-verifica: entre una vez y otra, alguien pudo desinstalar o
    ' actualizar esa instalacion. Confiar a ciegas en el archivo recordado
    ' reintroduce el problema que este archivo viene a resolver.
    If py <> "" And fso.FileExists(py) Then
        If verif <> "" And TieneDependencias(verif) Then
            LeerRecordado = py
        End If
    End If
End Function


Sub Recordar(pythonwExe)
    Dim ruta, f, consola
    ruta = RutaRecordado()
    consola = fso.BuildPath(fso.GetParentFolderName(pythonwExe), "python.exe")
    On Error Resume Next
    If Not fso.FolderExists(fso.GetParentFolderName(ruta)) Then
        fso.CreateFolder fso.GetParentFolderName(ruta)
    End If
    Set f = fso.CreateTextFile(ruta, True)
    f.WriteLine pythonwExe
    f.WriteLine consola
    f.Close
    On Error GoTo 0
End Sub


Function ListaCandidatos()
    ' Orden: primero lo explicito, despues pyenv (la version activa, NO el
    ' shim: el shim lanza DOS procesos y despues hay que adivinar cual matar),
    ' despues instalaciones normales.
    Dim opciones, salida, ejec
    opciones = Array( _
        sh.ExpandEnvironmentStrings("%OPTIMEM_PYTHONW%"), _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\python.exe"), _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\python.exe"), _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\python.exe"), _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python310\python.exe"), _
        "C:\Python313\python.exe", "C:\Python312\python.exe", _
        "C:\Python311\python.exe", "C:\Python310\python.exe", _
        sh.ExpandEnvironmentStrings("%USERPROFILE%\venv\Scripts\python.exe") _
    )

    ' pyenv: primero el comando, y si no responde, se escanean las versiones
    ' instaladas. El comando no siempre se resuelve desde el entorno de
    ' WScript aunque este en el PATH del shell.
    On Error Resume Next
    Set ejec = sh.Exec("cmd /c pyenv which python 2>nul")
    If Err.Number = 0 Then
        salida = Trim(ejec.StdOut.ReadAll())
        If salida <> "" And fso.FileExists(salida) Then
            opciones = Unir(Array(salida), opciones)
        End If
    End If
    On Error GoTo 0

    Dim raizPyenv, ver
    raizPyenv = sh.ExpandEnvironmentStrings("%USERPROFILE%\.pyenv\pyenv-win\versions")
    If fso.FolderExists(raizPyenv) Then
        On Error Resume Next
        For Each ver In fso.GetFolder(raizPyenv).SubFolders
            Dim exeVers
            exeVers = fso.BuildPath(ver.Path, "python.exe")
            If fso.FileExists(exeVers) Then
                opciones = Unir(Array(exeVers), opciones)
            End If
        Next
        On Error GoTo 0
    End If

    ListaCandidatos = opciones
End Function


Function Unir(primero, resto)
    Dim n, i, out
    n = UBound(primero) + 1 + UBound(resto) + 1
    ReDim out(n - 1)
    For i = 0 To UBound(primero)
        out(i) = primero(i)
    Next
    For i = 0 To UBound(resto)
        out(UBound(primero) + 1 + i) = resto(i)
    Next
    Unir = out
End Function


Function TieneDependencias(pythonExe)
    Dim ejec
    TieneDependencias = False
    On Error Resume Next
    Set ejec = sh.Exec("""" & pythonExe & """ -c ""import psutil, sklearn, fastapi, uvicorn""")
    If Err.Number <> 0 Then Exit Function
    Do While ejec.Status = 0
        WScript.Sleep 50
    Loop
    TieneDependencias = (ejec.ExitCode = 0)
    On Error GoTo 0
End Function
