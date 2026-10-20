"use strict";
const erro = new URLSearchParams(location.search).get("erro");
const msgs = { senha: "Senha incorreta.", bloqueado: "Muitas tentativas. Espere 5 minutos." };
if (erro) document.getElementById("erro").textContent = msgs[erro] || "Erro ao entrar.";
