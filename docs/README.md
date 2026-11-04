# Modos Agente y Multiagente de AudiAI

AudiAI permite escoger el nivel de profundidad de la revisión antes de iniciar
una auditoría. Ambos modos trabajan dentro de la carpeta autorizada, usan
lectura de solo lectura y solicitan permiso antes de acceder a archivos
confidenciales o compartir evidencia con un proveedor externo.

## Agente

Usa un único rol de análisis para revisar la evidencia y preparar el informe y
las recomendaciones locales. Es la opción más rápida para una primera
revisión o cuando se necesita una respuesta directa.

## Multiagente

Coordina roles especialistas por etapas: seguridad, investigación de causas,
procedencia, documentación, remediación y revisión final, además de las
comprobaciones deterministas. Aporta más perspectivas y comprobaciones, pero
puede tardar más porque ejecuta más etapas.

## Descargar el informe

Al finalizar la auditoría, el panel **Resultados → Informe y fuentes** muestra
**Descargar README**. El archivo descargado es un informe Markdown que resume
el modo, los hallazgos, las recomendaciones, las limitaciones y las evidencias
citadas.

El informe no incluye fragmentos de archivos ni rutas absolutas. Las
evidencias asociadas a archivos confidenciales y los resultados que dependan
de ellas se omiten de la exportación, incluso si se autorizó leer esos
archivos durante la sesión. Las conclusiones son propuestas para revisión
humana, no una garantía de seguridad.

## Adjuntar el proyecto como contexto

En la aplicación de escritorio, selecciona una carpeta con el botón de
contexto. Su nombre aparece sobre el campo de texto; seleccionarla no inicia la
auditoría ni añade la ruta a la conversación. Escribe una instrucción y envíala
para iniciar la revisión con esa carpeta. Antes de enviarla, puedes quitar el
contexto con **×** o seleccionar otra carpeta.

## Modelo y privacidad

El identificador del modelo se muestra sin el nombre del backend. Con LM
Studio, las solicitudes al modelo permanecen en la instancia local configurada.
El uso de un proveedor externo requiere consentimiento explícito antes de
enviar evidencia.

Las instrucciones internas de cada rol se mantienen en
[`backend/app/prompts/agent_prompts.md`](../backend/app/prompts/agent_prompts.md);
este documento describe el comportamiento desde la perspectiva del usuario.
