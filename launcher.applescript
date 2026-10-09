-- Resolve Splash Patcher: abre la interfaz sin mostrar Terminal.
-- La app se queda abierta mientras el programa corre; cerrarla (Cmd+Q) lo apaga.
property serverPID : ""

on run
	set binPath to (POSIX path of (path to me)) & "Contents/Resources/ResolveSplashPatcher"
	try
		set serverPID to do shell script "nohup " & quoted form of binPath & " --background >/dev/null 2>&1 & echo $!"
	on error errMsg
		display dialog "No se pudo iniciar Resolve Splash Patcher:" & return & errMsg buttons {"OK"} default button "OK" with icon stop
		quit
	end try
end run

on reopen
	-- clic en el icono del Dock: nada que hacer, la interfaz ya está abierta en el navegador
end reopen

on idle
	if serverPID is not "" then
		try
			do shell script "kill -0 " & serverPID
		on error
			quit
		end try
	end if
	return 3
end idle

on quit
	if serverPID is not "" then
		try
			do shell script "kill " & serverPID
		end try
	end if
	continue quit
end quit
