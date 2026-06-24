(function() {
    "use strict";
    if (window.__TAKEOUT_SPY_INJECTED) return;
    window.__TAKEOUT_SPY_INJECTED = true;
    const SOURCE = "takeout-downloader-spy";
    function post(type, data) { window.postMessage({source:SOURCE,type:type,data:data,href:location.href},"*"); }
    function scanText(text) {
        if(typeof text!=="string")return[];
        return Array.from(new Set(text.match(/https:\/\/takeout-download\.usercontent\.google\.com\/download\/takeout-[^"'\s<>]+\.zip(?:\?[^"'\s<>]*)?/g)||[]));
    }
    function reportUrl(url) {
        if(url&&(url.includes("takeout-download.usercontent.google.com")||url.includes("manage/archive")||url.includes("TakeoutApiUi")))
            post("url",{url:url,time:Date.now()});
    }
    function reportResponse(url,text,obj) {
        var fromText=scanText(text),fromObj=obj?scanText(JSON.stringify(obj)):[],all=Array.from(new Set(fromText.concat(fromObj)));
        if(all.length) post("urls",{urls:all,sourceUrl:url,time:Date.now()});
    }
    var origFetch=window.fetch;
    window.fetch=function(){var args=arguments,u="";if(args[0])u=(typeof args[0]==="string")?args[0]:(args[0].url||String(args[0]));reportUrl(u);return origFetch.apply(this,args).then(async function(r){try{var c=r.clone(),ct=(c.headers.get("content-type")||"").toLowerCase();if(ct.includes("json")){var o=await c.json().catch(function(){return null});reportResponse(u,"",o)}else if(ct.includes("html")||ct.includes("text")||ct.includes("javascript")){var t2=await c.text().catch(function(){return""});var m2=t2.match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g);if(m2&&m2.length)post("filenames",{filenames:Array.from(new Set(m2)),sourceUrl:u,time:Date.now()});reportResponse(u,t2,null)}}catch(e){}return r});};
    var lastXhrUrl="",origOpen=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){lastXhrUrl=u;reportUrl(u);return origOpen.apply(this,arguments);};
    var origSend=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(){var self=this,u=lastXhrUrl;function onReady(){if(self.readyState===4){try{var ct=(self.getResponseHeader("content-type")||"").toLowerCase();if(ct.includes("json"))reportResponse(u,"",JSON.parse(self.responseText));else{var m3=self.responseText.match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g);if(m3&&m3.length)post("filenames",{filenames:Array.from(new Set(m3)),sourceUrl:u,time:Date.now()});reportResponse(u,self.responseText||"",null)}}catch(e){}}};self.addEventListener("readystatechange",onReady);return origSend.apply(this,arguments);};
    setTimeout(function(){document.querySelectorAll('script[class^="ds:"]').forEach(function(s){var t=(s.textContent||"").match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g);if(t&&t.length)post("filenames",{filenames:Array.from(new Set(t)),sourceUrl:"ds-scripts",time:Date.now()})})},100);
})();
