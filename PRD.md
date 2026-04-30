# Product Requirements Document (PRD): Biting Lobster

## 1. Información Clave (Metadatos)
- **Título del Proyecto:** Biting Lobster
- **Versión:** 1.0.0
- **Estado:** POC de MVP.
- **Autor (Tech Lead / PM):** Charly Kano
- **Enlaces Relacionados:** [Enlace a Figma / Diseños de Guest Home]

## 2. Contexto y Problema
En el contexto del mundial de la FIFA 2026 muchas personas desean adquirir boletos pero estos se agotan inmediatamente. Es
por ello que se necesita de una herramienta que permita conseguir estos boletos sin romper la seguridad o infringir alguna ley. 

Se requiere de una aplicación automática y semi autonoma que, mediante parametros básicos de configuración, asista la operación de Login, permita configurar los criterios de busqueda de boletos, monitorización, agregar al carrito y notificación para que el usuario pueda concluir la compra.

## 3. Objetivos y Métricas de Éxito (KPIs)
- **Objetivo de Negocio:** Lanzar una aplicación que busque los boletos de los equios o partidos de interes y los agregue al carrito de compras.
- **Métricas de Éxito (SMART):**
  - Adquisición de Usuarios: Acanzar 100 instalaciones totales desde el repositorio oficial, sitio oficial y/o tienda de aplicaciones.
    S: Instalaciones desde repositorio oficial, sitio oficial y tienda de aplicaciones.
    M: 100 instalaciones.
    T: 40 días o hasta 1 día antes del mundial.
  - Retención y compromiso: Incrementar los usuarios activos a un minimo de 10 diarios.
    S: Incrementar los usuarios activos
    M: 10 diarios
    T: 10 días antes del mundial
  - Monetización: Lograr una tasa de conversion de usuarios de 5% de usuarios gratuitos a los que pagan.
    S: Conversion de gratuito a paga.
    M: 5%
    T: 10 días antes del mundial
  - Desempeño técnico: Debe pesar menos de 200 Mb, responder inmediatamente, rapida en monitoreo y notificación en menos de 2 segundos.
    S: Ligera, responsiva, prioritaria e inmediata para notificar.
    M: Menos de 2 segundos.
    T: Continuo hasta el día antes del mundial.
  - Satisfacción del cliente: Debe conseguir al usuario al menos 1 boleto en su carrito y si su opinión es positiva, debe ofrecerle hacer un donativo. También debe de aparece la opción de no funcionó.
    S: Conseguir al usuario al menos 1 boleto y la opción de obtener más.
    M: La tasa de No Funcionó es del 10%.
    T: Continuo hasta el día antes del mundial.
## 4. Personas de Usuario y Casos de Uso
**Persona Principal:** Persona que desea conseguir un boleto para uno o varios partidos del mundial.

**Historias de Usuario Core:**
- *Como* usuario, *quiero* sentirme seguro al iniciar sesión directamente en FIFA desde mi propio Google Chrome, sin entregar mis credenciales a la aplicación.
- *Como* usuario, *quiero* que la aplicación use la sesión que yo ya inicié en Chrome (vía conexión CDP) y solo en caso de captcha me pida resolverlo manualmente.
- *Como* usuario, *quiero* que la aplicación me pregunte en que equipos o partidos estoy interesado o si solo quiero un boleto del partido que sea y recordar esta decisión.
- *Como* usuario, *quiero* que la aplicación busque, monitoree y agrege al carrito automaticamente los boletos que cumplan con mi criterio de interes, de un boleto a máximo 2.
- *Como* usuario, *quiero* que la aplicación me notifique en Telegram y en la bandeja del sistema cuando haya logrado agregar boletos al carrito de compra exitosamente e indicarme en el mensaje que partido es, cuanto cuesta y cuanto tiempo me queda dandome un acceso directo o una manera de ir a revisar el carrito.
- *Como* usuario, *quiero* que al ir a revisar el carrito la aplicación me entregue el control para poder concluir la compra por mi propia cuenta de manera que mis datos bancarios no son expuestos a la aplicación.
- *Como* usuario, *quiero* poder consultar qué está haciendo la aplicación (su estatus) y ser notificado de los problemas que enfrente.
- *Como* usuario, *quiero* que si he conseguido un boleto en mi carrito pueda agradecerle al desarrollador con un donativo.
- *Como* usuario, *quiero* que si la aplicación ya está configurada se inicie automaticamente al iniciar el sistema operativo.
- *Como* desarrollador, *quiero* que la aplicación reporte las incidencias de manera remota para poder consultarlas y corregirlas.
- *Como* desarrollador, *quiero* que la aplicación registre información analitica básica para enteder su uso.

## 5. Requerimientos Funcionales (MoSCoW)
**Must Have (Obligatorio para el POC):**
- Flujo de onboarding (Instrucciones, apertura del Browser para asistir el Login, configuracion de criterios de busqueda de boletos de interes, guias para configurar de nuevo estos valores).
- Home (Pantalla principal donde se muestra la actividad de la aplicación, opciones para detener y arrancar, configurarla y visualización de los boletos que puede conseguir gratis (1)).
- Modulo para conseguir más boletos dando un donativo (solo puede conseguir 1 más como máximo o pagando  para conseguir 10 adicionales como máximo) pero siempre el total es 40 como máximo por usuario.
- Pasarela de Pago (Checkout) usando Ads de video, Buyme a Coffe, Paypal o integraciones nativas.
- Configuración con botón de Eliminar Cuenta y enlaces legales.
- El producto distingue dos vías para ampliar el límite de boletos que la aplicación puede intentar asegurar en carrito, ambas basadas en donación y reflejadas en el registro remoto del usuario (access_granted / límites asociados en el sistema de licencias).
(A) Desbloqueo asistido por la aplicación: el usuario utiliza los enlaces integrados (p. ej. Buy Me a Coffee y PayPal) desde la propia app; la donación debe incluir en el campo de notas el identificador de dispositivo indicado por la app, de modo que el sistema pueda correlacionar el pago con la fila de licencia correcta. La actualización del límite en el servidor puede ocurrir con latencia después del pago.
(B) Desbloqueo fuera de la aplicación: el mismo resultado de ampliación puede lograrse cuando el operador del sistema actualiza manualmente el registro de licencia (p. ej. donación por otro canal, soporte, o proceso interno). Esta se usará por lo regular para asignar el valor FULL a access_granted desbloqueando la máxima capacidad. La aplicación no expone un flujo separado de “donación explícita” más allá de los enlaces y mensajes de instrucción; la ampliación se observa al sincronizar el estado remoto. La asignación manual de FULL requiere validación interna y registro de auditoría.
Otros requerimientos:
- Debe conseguir al menos 1 boleto en el carrito a cualquiera que la use.
- Debe de funcionar unicamente hasta un día antes del mundial.
- Debe ser más rapida que si lo hiera un humano de manera manual con un browser.
- Debe de conseguir al menos 10 personas que agradezcan con donativo monetario o 1000 USD al permitir conseguir más boletos pero siempre el máximo será 40 boletos en total por usuario y despues ese usuario se bloquerá. La aplicación deberá de informarlo y permitir usar otro usuario.
- Debe ser rápida en especial en el horario local a las 9 am ya que a esa hora suelen aparecer boletos.
- Debe agregar el boleto al carrito de compras, máximo configurable por sesión por usuario y notificar al usuario por Telegram inmediatamente que logró agregar al carrito.
- Debe ser compacta y ligera, facil de instalar y de desinstalar.
- Debe poder funcionar en Mac OS la primera versión pero ser compatible para liberarse poco despues en los 3 principales sistemas operativos: Windows, Mac y Linux.
- Debe de permitir al desarrollador de la aplicacion controlar quien puede usar la aplicación (no se desea permitir el abuso), donde puede usuarla (solo en México, Estados Unidos y Canadá) y cuantos boletos puede consegir en el carrito.
- Debe solicitar únicamente los datos y permisos mínimos: número Telegram, permiso de ubicación e internet.
- No debe solicitar ni almacenar usuario/contraseña de FIFA ni datos bancarios.
- No debe realizar ataques de denegación de servicio (DoS). Debe respetar los intervalos de refresco para evitar bloqueos de IP (Shadowban).
- La inversión para esta aplicación debe ser minima o cero priorizando el uso de herramientas, libres, o de pago dentro de sus limites freemium.


**Should Have (Altamente Deseable):**
- Por definir

**Could Have (Agradable tener, si hay tiempo):**
- Notificaciones mediante WhatsApp.
- Notificaciones Push (Firebase Cloud Messaging) en una app React Native.
- Máximo 10 instancia ejecutandose mundialmente par el mismo partido o boleto.
- La actualización de access_granted se deberá realizar desde una Edge Function (RPC) en Supabase para proteger la escritura de la tabla y usar RLS (Row Level Security) asi se evita que un usuario malintencionado pueda modificar su valor.
- Integración con Telegram para notificaciones y sincronización de sesiones usando una Edge Function en Supabase para mantener el Token seguro en el backend.
- Implementar estrategia con offset persistente por instancia para:
  evitar reprocesamiento de mensajes históricos,
  reducir tráfico innecesario al API de Telegram,
  mejorar escalabilidad multiusuario y consistencia entre sesiones.
- Puede obtener un boleto adicional viendo 2 anuncios (solo puede conseguir 1 más como maximo)
- Empaquetado para MacOS con Nuitka.
- Heartbeat de sincronización en Dashboard (timestamp de última consulta + evento periódico de estado) para depuración y visibilidad operativa.

**Won't Have (Ver sección 9).**

## 6. Requerimientos No Funcionales
- **Arquitectura:** Clean Architecture con MVVM y Unidirectional Data Flow y Principios SOLID.
- **Manejo de Moneda:** Implementación estricta de patrón *Zero-Decimal* en USD (los precios se procesan en centavos a nivel de código).
- **Seguridad y Privacidad:** La app no recolecta credenciales FIFA ni datos bancarios. El login se realiza manualmente por el usuario en su propio Chrome y la app solo reutiliza el `storage_state` local. Incluye aviso de privacidad resumido en Onboarding y cumplimiento básico de normativas y leyes.
- **Disponibilidad:** Soporte para MacOS y escalabilidad a Windows y Linux.

## 7. Diseño y Experiencia de Usuario (UX)
- **Componentes Core:** Uso de tarjetas estructuradas y paneles compactos.
- **Estilo:** Basado en el estilo de un Wizard o similar y su operación puede ser como un servicio de activo en la bandeja del sistema.
- **Tema:** Basado en Material 3 utilizando los colores de la FIFA y los de la Selecciones de Futbol de México y Estados Unidos.
- **Jornada del Usuario:**
  1. Splash & Onboarding.
  2. Modo "Navegador Preparado": El usuario abre manualmente Google Chrome con puerto CDP habilitado y realiza login en FIFA.com. La app se conecta a esa instancia para capturar la sesión.
  3. Configuración de Criterios: Selección de partidos.
  4. Modo "Cacería" (Background Service): La ventana se cierra o minimiza. Playwright empieza el loop de monitoreo usando las cookies capturadas.
  5.Hit!: Notificación Telegram/Sistema + Sonido de alarma.
  6. Checkout: La app vuelve a abrir el navegador en la página del carrito y le entrega el mouse al usuario.
  

## 8. Riesgos, Supuestos y Dependencias
- **Riesgo:** Algún posible infringimiento de alguna ley lo cual hay que evitar pero al mismo tiempo brindar la posibilidad a las personas de conseguir sus boletos ya que hay muchas personas que por su trabajo no pueden estar esperando o buscando todo el día y los acaparadores y revendedores aprovechan esta situación por ello los limites sanos de uso y de ganancia de esta app. Cuando se consiga ayudar a las personas y se consigan los ingresos previstos se habrá cumplido el ciclo de esta aplicación.
- **Dependencia:** Integración pagos (de las que tengan facilidad de integración coimo Buyme a Coffe, Paypal, Stripe, Crypto o APIs de pago), Monitoreo de errores similar a Crashlytics, API de Telegram para enviar notificaciones o alguna alternativa gratis, Analytics como los de Google o alguna alternativa gratis y simple ya que la aplicación no es compleja.
- **Supuesto:** Los usuarios están dispuestos a realizar transacciones digitales para conseguir más boletos para familiares y amigos.

## 9. Fuera de Alcance (Out of Scope)
Para prevenir el *scope creep* y asegurar el lanzamiento rápido del MVP, quedan excluidos de esta versión:
- POR DEFINR despues del primer analisis del Plan