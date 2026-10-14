# Mount boundary repair

**Assigned repair complete; broader Linux gate remains HOLD.** Full C: **1352 passed / 66 skipped**. Final nonroot WSL mount/guardian/discovery: **251 passed**. Future Compose only; no live changes. Broader Linux dependency/test blockers and unchanged raw-CRLF shell failures are retained below, not waived. Final hashes and scope evidence appear in “Disposition and limitations”.

Exact checkout: `C:/Users/User/_projects/takeout-downloader-reliability`.
Premise verified: default webGUI recursive STORAGE_ROOT=/opt and both CLI /opt binds expose the mandatory host EnvFile parent. Future configuration correction only; no live actions.

## START source/config/test fingerprints
173-file baseline matches prior END; no rebaseline of prior documentation gate. Commander identifies prior wiki banner drift as deliberate.
```json
{
  "Dockerfile": "5e435a2cb23d37ac289c2897d90c75c3aeba79b8657fad1c08cdb831cabf9fda",
  "Dockerfile.takeout_dl": "b768345e0a87772b4a8b3ec8c18b260b1f876eb7764bc125894d1836b2996fd5",
  "aria2c_integration.py": "9faa16a9d0afa1d6c914665fe891c0695aa976809862c528d045d81115720ac9",
  "autopilot/__init__.py": "ffaa24a9d0e988b9bf5fd246354ae38fa67e93c4a276c0346bb37fe8717a59ce",
  "autopilot/__main__.py": "deeb474593b0bb6d2b871699e931018169cc72f0a6944ee035a68b703d72cead",
  "autopilot/cdp.py": "022b06529c8f949abce8d705fc7009b54e0639d45cc5413bd6f95a6e30325173",
  "autopilot/discovery.py": "93d601df0280893937268eb66a52234954d3b70a332e2306fc88011b3219130b",
  "autopilot/errors.py": "03044e0dde01de5af5dbf9e497274bd39a83a66d18cca09f0496c5a5778fc097",
  "autopilot/jar.py": "9e675020ad63fa14bff6663ccedcc5c5dec154896468f44762d6441604726484",
  "autopilot/ledger.py": "f72dfcb23b9d81e70bd1c6b3146b9900dc52ce705a2fa85f7ac3e6eb38b76629",
  "autopilot/mint.py": "eaff75151ec6c58e265984e72ddbd204dac70ec19fc48e894a80ad1464c16fb0",
  "autopilot/mover.py": "b4659a477441e076060148ddefa9cdd1b489c11700cea4fa274d1057b976cb56",
  "autopilot/remote.py": "7c474340b436b29f209131d0608ff6600b85f90d5fc48e2eb293a1805c84d72c",
  "autopilot/report.py": "9d7752da3eb9ef239f1e9b83318ea487bf64703714242d93f20644ca448ffa60",
  "autopilot/run.py": "7208d37f5b0c8bcc83a70a55a52653dbc6d998b12403ccca71601dd50779a3ed",
  "autopilot/scrape.py": "f28abd58d820a84626b1b083622731311e2d1561879414e35c80f5b3cd056292",
  "autopilot/transport.py": "9188b520c34a070c16505010264f46e7af771f9614713217e54c480df5bbf47d",
  "autopilot/verify.py": "594286ba82a23968d3b7021a1ce16f7f1816a7b9ce8d025a9ac49191bc870f0e",
  "autopilot/watch.py": "4f6381d43797eb3f839ea288b3937024b73cb4441f21720fb515ae59bf7404c2",
  "autopilot/ws_transport.py": "283709d867d9c6190c390e53b634cc75d039300a397faaf945f5d680e99cccc5",
  "build.py": "106c511ebc6299b16069637f4cd39422a35d67e73f51a1249168f932a7eba930",
  "cdp_reload_ext.py": "a614c70df47c02d161516cf77dc78be4e58d826585a4a833597fe443625e0c41",
  "conftest.py": "855ab46fb3034b4a3baa5ceb992358dc1bea52502665f71ef998953474ef775a",
  "dedupe_takeout.py": "420d31c166dda9dfbb1e1fb99bd0c5e86b41edd8f21253e12ddc03df9f0a60e8",
  "deploy/takeout-autopilot-watch.service": "3a073ea144716e6cf74dfbd818649b866d2767d7d44bc352426c6a595739d3dc",
  "deploy/watch.example.json": "3d6b5a73e5cc25ccb10643cb2e1b4217c5f7587a83392d31071bd72901a258a7",
  "docker-compose.webgui.yml": "a3ffdab098a773a9c1e1935c3a87c44f9ae10ee9fd9dc39964df0103e4fc7a43",
  "docker-compose.yml": "bc0afa72a1871ca798ee717b562ed977756c198fe6e8d5da72920647cd56536a",
  "google_takeout_tui.py": "0f05b580c12f304e042ace2ff2b32dbf6dcbaea5a7787064d3bf49b1d786bdee",
  "helpers/manifest.json": "a7fe5b3ff15ad069abc0bd0b17650b7b52b54231167adcd57562281a93795cc1",
  "manager/__init__.py": "f76587ca210b223751f87c250ef619ea84d4391c5fecc679c27c0035c4221cc5",
  "manager/__main__.py": "1ee571c0408fa568b92ca4ddaaa3be87078ac684994ec8f0be48eedd2185850f",
  "manager/app.py": "e7d0c3b32c615cbb00235f850c148ac9df236c190a36e9874370610fe298e95f",
  "manager/config.py": "63d5148f2e948d325946d2e68112cc9381cecbe52113403bd5fde301deafa36a",
  "manager/derive.py": "5aa5da8c9c87d06f56ec2ae1bf0dd6f1567f1520a12408c5babdc63caa8901c6",
  "manager/diagnose.py": "0ac2ad445befe22c6b15846f2c26d31b183a084ba9411a4c0f2d31aa311398dd",
  "manager/engine_bridge.py": "91523c1d7484e2c1752bc47ebf288d1bbb36f98320f19d1d4db88a4f372c37a4",
  "manager/jobs.py": "3e56cc3be6dd3f5e05b4b0c1f425bfabd1898e79098c69366b7d584a53049540",
  "manager/manifest.py": "b1859e6c56e83151c80669dad5beaa72c5688777dfbe0d957197aa23bf268691",
  "manager/notify.py": "265d41dd94d74f294534088c8c1c1ff0843446b59cf4b215dded8862d8d8e04e",
  "manager/orchestrator.py": "ff2d7ae2854a9c087da69bfaca1a1abcdf375e8d8de4213edd401495fa24c1a7",
  "manager/recipes.py": "5bf833f21e9e95381aac312af617bf2cd7d569f025f95af14dad76b15ab4ab01",
  "manager/requirements.txt": "e87473ee80bd56be8ca39b0eb55d46a0ae6a36c06e8a079269409020b49f695c",
  "manager/tests/test_e2e_phase2.py": "a9e105f0a308c0465cd6a25352fa3f0c6274ae805ac63accce3dd51b90533983",
  "manager/tests/test_phase4_control.py": "e6b48ced9bafd894885860ca8661e9a0c04f5ffe9663c3c004d06b849d224dc8",
  "manager/tests/test_phase5_v4contract.py": "f7a731a2b5fbe432ec4ef53dffc8775cfc4ea9a5c029f5d172edf4af8ad85fa0",
  "manager/tests/test_phase8_telegram.py": "265eb39812d0822858d22ff7f8a7e55e9c45a5919ecb7aaee4c65983761e7d1c",
  "manager/tests/test_phase9_recipes.py": "67a3e2a0fc33d4ce9155ba4fc520df9fee5c446aa4a49456d121fb26b7dda5b3",
  "manager/tests/test_review_fixes.py": "0e18421221b68c7646da21b861beab63b26b7c982f57bd8df2fa2b4002ebdcf3",
  "manager/v2integration.py": "99afea101addd8c974f59d53937cd4ec1e9c6464320b18d70db4b4e5ea7f2d4b",
  "monitor_job.py": "7d8ed73598acb08f2d3ce15b68a0beeffd12ec699f037a70289345ba9e73ee65",
  "paste_server.py": "82cddc318914d7cce709193fe8329bfd3f85d3afc1966b287a41ab616b8397f0",
  "pytest.ini": "ca4b82760538130959a0fbe25c21fc5d28cd46fb7a63b1209def6d19e2b40a62",
  "requirements-autopilot.txt": "24be066fd8d53d76c9d14033dae93d05755d75a11808d179f454b4ef36495df1",
  "requirements-dev.txt": "c7c71a42aa7fcda7d4b026f6aeb29e93265af099ed49d922023ea139cc43e0da",
  "requirements.txt": "cd0ac27fb61eaab8cac6427248ed57d6c02d5299ee5ae6a4d15691c4c8d19a06",
  "security/__init__.py": "bb5907ad0c992dcc380137fcf7888ed66762bb9000ca5ab13ede4368077de5b9",
  "security/c2_resolver.py": "1895f14aa2286f5d81fdc6608635e0368d527679443d4c5488661a510044bbd4",
  "security/detector.py": "7bc92a83148fc6f2651e810fc9f4fb9f45cfd1b933f26239179b28931dd759d0",
  "security/fixture_generator.py": "5bd9822f9d76a3b02d93feb93c9fea55293fb8ade11e828f0156e14a57ec16e1",
  "security/obfuscation_lab.py": "b57e81df276e552161905a0d11849f85ed8fe9f1406eaea7cd4e6f719a53caf2",
  "security/sandbox.py": "53a5197d6c426700254498ccc514541a505e83232996d24ffe6b5593d597044f",
  "takeout.py": "fca317e73eb591d0ba5dee7e86dbd071719396629bad9a823d1c3f7222006b05",
  "takeout2/__init__.py": "dcbfc057afbcd232bfc71a49c082ff9e1c461ebb62583997501d86652332eae0",
  "takeout2/__main__.py": "7328bda130ffaabe956dbcc8b0be95b7f514350536b638f7e605bf285d1ef188",
  "takeout2/api.py": "7d476f4628c8af5a1ead80b82dc12ca1b44d59b4a786712e2a20e7a44eeecdd4",
  "takeout2/backoff.py": "98b5fbaa847271177183321dd0197d3ed8a9227149e10e29645e5116ace83f22",
  "takeout2/cachewatch.py": "5052a21a430f220877e6e1ea114762cba23319bcae96708e55a56593c0fab6aa",
  "takeout2/classify.py": "637d0bdb767246738534dae0517070c1b4da6c8ebfa5590c5a9a802f40b1940f",
  "takeout2/cli.py": "64055fa8c1d5948e03fce3f88ae16729bc4ce193e5713a7933c09a04987d1b30",
  "takeout2/contracts.py": "b6772872ef5397abc53e687a8e5c8c6b8b4d3c27c25456b186f9efc0234bdf17",
  "takeout2/cookie.py": "50032ff97e775047533f4c4fa23577c0d3f26b1678e2e25b5c9f724fdea63605",
  "takeout2/engine.py": "d10d1d68276e5b2d965a18c51469e24499baf38740c7e292deb305ec2a5bfb59",
  "takeout2/experiment.py": "5ec5f07bbc7e1ec2cfd5bda0c65cce3f67e174f795399256eb1457a176f7f318",
  "takeout2/ledger.py": "39bae33c451f2dce766f3029864c4ed74df6abff0e54fdbfa29a6eb67418f68e",
  "takeout2/migrate.py": "79ef4b76e6e859e8ec6b61584ba6ce26b945da197e6ef2cc391973682c537cf8",
  "takeout2/plan.py": "22a9c32aad8d40374aec25df9758485b447ceae5bebf330af1ef24b9f60ceb47",
  "takeout2/preflight.py": "21b78d068b35afdb44c2f77826f03ba288157813e7166d1e59c2f13cb5e9a300",
  "takeout2/progress.py": "ccf3d16f961d84c11e557159bbce109a0337bae008ec994024ed4acc72fc2b10",
  "takeout2/runner.py": "b8d818b3c32c5b67083e0fcfe17c6e7668374fd01f129271bd4d2a91aba1c6be",
  "takeout2/state.py": "be9e6d34de925d22a5cc2a02f5ed930e99dda0224d9781fee0c2a2248ab61d47",
  "takeout2/verify.py": "c92a52bbbc1e1eb03145ec1375d271f63e8b8261410e589806e6e654b589d82e",
  "takeout_cli.py": "a06f33860791f3dff055221b58cf1919b546bb2c11175e69d3ded9da1522662b",
  "takeout_cli_analyze.py": "026008fe8c65c11c69180af8619b3a5efc6dd7aab5b205501d8433399a1bafb1",
  "takeout_dl.py": "4376a9ab755652445e324161650680dcba4e70d04412eeb7996b56f115a55822",
  "takeout_downloader.py": "4542c5bfa655fbc44ffac76df9515b6a57e6ee5803bd176bed7ec35e5a16c9f1",
  "takeout_payload.py": "94fee88e43b615ac427d603abf42b6b278cfbb5ace151b370af945f02175fd55",
  "tests/fixtures/sample_multi_payload.json": "dea786c8dda6a9ad8dec552f193a005fa6d8f4b37341c9c5523b9ebc5fef6c49",
  "tests/fixtures/sample_payload.json": "33c1881b18fc31a104fc9381985062447ce7506f63185fc23e24ff08be07b6a9",
  "tests/test_internal_downloader.py": "21729f4413d1384a064e5b18822676105ee1b8bd8bc60ced2a4d1fab4103bbfa",
  "tests/test_paste_server.py": "d5549d8510b77ee72759eebe16e07fc18f915f4f843b044270e5b73214d70fcd",
  "tests/test_security.py": "f10f335cb00fe6d918937071a8924aaf93def580550610f4bc6985aac1fd6a66",
  "tests/test_takeout_cli.py": "6856ef29214fd0e464cd24cf15209c492403b2063d70d59074b7ef23186b4ca1",
  "tests/test_takeout_payload.py": "25b9931f17f50565cc908c10dbf407383acf060a3da285609e9786137270fca5",
  "tests/v2/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "tests/v2/test_api.py": "708dd4d146d8a68b276fd87eac1d7af3dc1d3dee9db4332b2f984a918a28a6ce",
  "tests/v2/test_autonomy.py": "d3e8693a7c842fa428f6c660fb7501d003f6f2243edf5fbf6b21306593f1912b",
  "tests/v2/test_backoff.py": "9c38065913cfe87a27518c06883de0487aa860d7fdb7a499b12750b25ddff1b2",
  "tests/v2/test_cachewatch.py": "d16a4b86d70a143b8b38f08b6065781ae0eaae5848c087637a6f585e8ff453cf",
  "tests/v2/test_classify.py": "6b125bb2337a72f1fe2880d367e6d507b20677c8817372cccefb750290baf637",
  "tests/v2/test_cli.py": "41df9876e132a0b6d91406fa9c5923429b937982c4eabc5178ec92e6f642f3ce",
  "tests/v2/test_cli_ux.py": "a3bf29ef952d80f1e577a7baa678c993c3bb720126f10dc378d964b2e52b088f",
  "tests/v2/test_contracts.py": "892c491a33ef0858eb2586eb88c589a0832fb4664357741b5f647d3e74f91867",
  "tests/v2/test_cookie.py": "11913fb03ccc8414da9038b29f08449145ea2d4fc1e0252312f79a4384c416b0",
  "tests/v2/test_engine.py": "598357e546b7e8468925a7acadd97c357b6d3307e3e731f431cb697a7274caf8",
  "tests/v2/test_engine_hardening.py": "2b329b96684f923ce997dce23891d62eb0bd317f7f8d8b4b9af98220e2bd44c3",
  "tests/v2/test_experiment.py": "4a53e942cfcf969639ad09df649f573a872889ab3e1552379fa70adb7980cfcb",
  "tests/v2/test_ledger.py": "4dfb645eb4809210167ace8a151a69e12b172c8155c4618770e039875779c9a1",
  "tests/v2/test_migrate.py": "e6c7c570347b871d66690acece90aa54b99ae481a83e620ae7fdd31083ad7c06",
  "tests/v2/test_pipeline.py": "6bec4dd0a9f038d472fb6608c80215a0d97607bb3f55cd9fc979e5b00880a335",
  "tests/v2/test_plan.py": "6355eed3681b7e55062bad26a4079256c322e320d504e4a9767d41a0e29de4c5",
  "tests/v2/test_preflight.py": "b70383f7c72b6a188bd9769f862e84b1fa8edd707cec1647abbad233496b025d",
  "tests/v2/test_progress.py": "ba4257dc2147df03d1696a4f271ccdf144bf894928ce700f944e418b8adf3f9e",
  "tests/v2/test_runner.py": "c314b55964e2ed3e50d903b73e7e9aed311bfa55b7420c1088364a4c77573239",
  "tests/v2/test_state.py": "a7277a88f9633bf8c7ccdf45c726aafebb75b27e50e3ab33a53b03301e1e3088",
  "tests/v2/test_verify.py": "f5c59bee5415d25732e55e6fef7c6b3ead3c20d745d4e6c0725d7e39682a3cbf",
  "tests/v3/fake_cdp.py": "2243ab6e7c6cb2d52924074e0cfd983e42191dceda925cd9e0c8c07ab5bad180",
  "tests/v3/fake_http.py": "2e4847909334cc7ae3c8c6c96e972df793b19d79326cc96d83dc93d2038c13f0",
  "tests/v3/orchestration_fixtures.py": "5f54443a81e6d483a6f03d320b013cb7f1bd60a55a3ae536348cd0105c8c4ea3",
  "tests/v3/test_cdp_longrun.py": "2e7d1367be54f6ae49e8056c38cc423d2ceb357b11da5d5dac5b795f0c5842f0",
  "tests/v3/test_cdp_target.py": "4709c1c41dc33c19717ae6cd87d0553763003c5db1dfdcdde96499e75165faec",
  "tests/v3/test_cli.py": "45da3a71107792b8e0c9ede0b4846f29c4964c72753c858bff144d0ef4571fca",
  "tests/v3/test_cli_remote_args.py": "1430448829a6aff9d35f977c1195e0636aa92a9e978985df9980a1d3ac96fae7",
  "tests/v3/test_container_watch.py": "b05d2954365c01284ada7481d50b666f0a5f28fb8f2c4951fa6a720a86587e70",
  "tests/v3/test_destination_index_is_used.py": "3e8ef2f09ad78e1db6229ad9500892f17f6b3cf2caa934a64367de9ca30b5409",
  "tests/v3/test_discovery.py": "79a2a4f25ce29adc9d8f9ae8a14200d91e78b157174f037c2c47b4ea1db5b5c3",
  "tests/v3/test_exit_codes.py": "36d081418780e86184384ae3753677135bdb2b6f428ffe5be1ffa343d53ddee1",
  "tests/v3/test_extension_defaults.py": "6b716d66ff8c1872604d68df615f61fe59aa3b52e9062ccf0d52dae627eb4a07",
  "tests/v3/test_filename_encoding.py": "18314274f33a931bc5820311edee3ba0999b56f000b8eea80af1537c59118685",
  "tests/v3/test_http_client_live.py": "dcee53865ac79088c67a7c451d324435cf5c5bf4f05538173c9afc0b0f88be04",
  "tests/v3/test_imports_resolve.py": "a0139d2ea7da3ecf468fa9a503ded4fc915eb5b0524f675d9f3c5e4423d1c9ba",
  "tests/v3/test_integration.py": "adfc5dd6863d51cac5f7ccc8ad5666092b192b690b3394803cac84919b91529f",
  "tests/v3/test_jar.py": "977d83f59ba7cf607d61bc11cce8a3d040d8ae63ec8ab9cc4f98aa59fc44dbe2",
  "tests/v3/test_ledger.py": "e886037829f63ba001b14b299d174143f5edfbf364c40fab38f389798aec0a60",
  "tests/v3/test_mint.py": "a8e75c228f031e106d6ed487a0afd949632d7ed768949ef744f46fedb28d272e",
  "tests/v3/test_mint_only.py": "1c7a926c68baa74f44b9d0d0760af30081bce5f17d7429a5953a7efd7aa1ce59",
  "tests/v3/test_mint_stray_downloads.py": "da92b6dfbd22337cc5a1e02ba455f4704f511a458588c0654e32f0bbc6bba06f",
  "tests/v3/test_mover.py": "99bb68152380083f34eb10ea27416db08e6fdeae936d18f9664bf2eda378ae04",
  "tests/v3/test_mover_watchdog.py": "7db89d885a09e8c4ef51227f08fc175a419ead3a8673ca3276c1f5f2afc78ced",
  "tests/v3/test_orchestration_reliability.py": "ae021704948c732ed1c93b02e95703aceaf742fa58f87d6d5127ec667699a2e8",
  "tests/v3/test_part_filename_preserved.py": "5bb9fb6c291af887b211d60278ebe9e7fd55c81289e7f9b3459372b9bccbfd77",
  "tests/v3/test_provoke_rapt.py": "093a1a96e9d2dd8675c2e9a9562d14f7a17267d1e42839afca29334b5e5e1911",
  "tests/v3/test_quota.py": "76073a6bc495c3855268b8befec57e096be37e63107b00111020065cf97e1a6f",
  "tests/v3/test_rapt.py": "24c969c96cafb6e4bfed0bdd94819460c47c9679d124c6ea225696438857c01f",
  "tests/v3/test_refresh_and_filename.py": "94e1da0e166424cfe8a1a538a5364749bad84914351500c73d51adbd15615035",
  "tests/v3/test_remote.py": "62ec8337aad48719603829aa5b835552372fb6f8fd6765ce2f9cd91fad3a391e",
  "tests/v3/test_report.py": "a6b5b8e6b683c50be3c550be87c91872b8449aa871dd3fcd569edc9a485e9457",
  "tests/v3/test_report_attempts.py": "130112a327b39d00c6c904506c951a1888e81977bff8a451deb2a2e971b6d9af",
  "tests/v3/test_report_sees_the_archive.py": "e12e1b0fcb53d7d57b7498e6b766bf38def7553acb074732743410f269a84be3",
  "tests/v3/test_review_fixes.py": "ae440575030ec05921bd2ed7f29c3e5b10bd6c37d334d8b7b6261f9ac82d1e5e",
  "tests/v3/test_run_destination_guard.py": "b9328270b22f4522fdd465f3d8a6965ebed2643a33c308292421ed4fe025c282",
  "tests/v3/test_scrape.py": "a5e3d3ccc8e249ec562aa36fe12dcdd0cd6d74746f716544cc4d58f0a0e5240a",
  "tests/v3/test_signin_classification.py": "73f3178eeb19a67f96abc63a1ef4e5cd5b4b7c557bac68a20f26e0b8cc68deee",
  "tests/v3/test_transfer_integrity.py": "71ee8c6f2c25cf237f0dc818838ba8a018e02abbe0475bc3d3a3d12297abab44",
  "tests/v3/test_transfer_without_browser.py": "c4c3d7e93ac981c024b791ca4a05b29f0c7a2fc9a882ba6869e3a2d21bf7b28b",
  "tests/v3/test_transport.py": "be7f6ca1cc60a95ccabfd3cecce5c1e44f512e7eea2185bdb31798eb079b2dfc",
  "tests/v3/test_verify.py": "d882fce0efa5a3540b6a7e35d020ea950a1478f8faa2fdab78137d988f5cb3f7",
  "tests/v3/test_watch.py": "47f5ab9ca651a57ac39303cb05867e19191be229872ff278b2d5bc77b5da6a22",
  "tools/container_watch.py": "3a2fa1f7c8fe5ec8c93f373bc97c2a214f47f47155d9bc90292b1ffe8d4004e9",
  "tools/extension_switch.py": "e10c7bae805a95435d928d24ea944af9baad171be4caf2e7e57fa31104ed227a",
  "tools/repair_part_filenames.py": "8f651c7d7dc6571d94da750a772426ed43fb14a21583d92e5c210bb4f8a69a94",
  "tools/satisfy_login.py": "79668282962e9f211c227f61f801e5692160152a726e104d8180b24a05e8d944",
  "tools/sweep_downloads.py": "e3902cc80baa6e546a45416e65558ded304ca3c07e208602b2788c326f7c3e07",
  "tools/xwd_to_png.py": "b04d5ed48f4a919565f10cad8c704592dd78cabf5fea7190943caed519a7f737",
  "webgui/Dockerfile.webtop": "755c4f13a87f9f77dfc3a7111dba47dcfc9cd9721d62ad790bd11a780f70fda1",
  "webgui/chromium-service.sh": "e648aa1c6844b5b16d6d15a305556d2a78e981b6bfaff6e6650dbeeffdeaddfa",
  "webgui/cloudflared/config.yml": "3a4a5da59991711907c810f44981ab655dfe5ef3fb7468c2fc18316466cb15b1",
  "webgui/cloudflared/setup-tunnel.sh": "780ac490873df9eef70043bde73bb9a1549458c93c749ad6c05532a1addc1a76",
  "webgui/cloudflared/verify-exposure.sh": "8ce669fa1843d1752e3eca4a258c9e2cba8e128595338658e9b2077cabb25bce",
  "webgui/init_custom.sh": "f8615cfcfa6fc46660a110c55b02ca71ee01ecf1872cacb530320e1bc769f878",
  "webgui/manager-service.sh": "d482ba894a26c4690f0c9ab0d6c69d7fe9811c9df43a408d7c488195447cf8dd",
  "webgui/profile-seed/managed-policy.json": "3c06963bda1f0abbdc70821c1d8d27f73bd101d1581dd4ed25625283b77773a8",
  "webgui/reload-extension.sh": "a2da4273037559a89fc38f8ad59ee35d4f512444b67b04204a74f6add4246cf8"
}
```

## WSL Linux v3/v2 suites
Exact argv:
```json
["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", "id -u; python3 --version; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/mnt/c/Users/User/anaconda3/Lib/site-packages python3 -B -m pytest -q -rs -p no:cacheprovider tests/v3 tests/v2"]
```
```text
1000
Python 3.12.3

==================================== ERRORS ====================================
____________________ ERROR collecting tests/v2/test_api.py _____________________
ImportError while importing test module '/mnt/c/Users/User/_projects/takeout-downloader-reliability/tests/v2/test_api.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
C:\Users\User\_projects\takeout-downloader-reliability\tests\v2\test_api.py:12: in <module>
    ???
../../anaconda3/Lib/site-packages/fastapi/__init__.py:7: in <module>
    from .applications import FastAPI as FastAPI
../../anaconda3/Lib/site-packages/fastapi/applications.py:7: in <module>
    from fastapi import routing
../../anaconda3/Lib/site-packages/fastapi/routing.py:45: in <module>
    from fastapi import params
../../anaconda3/Lib/site-packages/fastapi/params.py:7: in <module>
    from fastapi.exceptions import FastAPIDeprecationWarning
../../anaconda3/Lib/site-packages/fastapi/exceptions.py:5: in <module>
    from pydantic import BaseModel, create_model
../../anaconda3/Lib/site-packages/pydantic/__init__.py:5: in <module>
    from ._migration import getattr_migration
../../anaconda3/Lib/site-packages/pydantic/_migration.py:4: in <module>
    from pydantic.warnings import PydanticDeprecatedSince20
../../anaconda3/Lib/site-packages/pydantic/warnings.py:5: in <module>
    from .version import version_short
../../anaconda3/Lib/site-packages/pydantic/version.py:7: in <module>
    from pydantic_core import __version__ as __pydantic_core_version__
../../anaconda3/Lib/site-packages/pydantic_core/__init__.py:8: in <module>
    from ._pydantic_core import (
E   ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
__________________ ERROR collecting tests/v2/test_autonomy.py __________________
ImportError while importing test module '/mnt/c/Users/User/_projects/takeout-downloader-reliability/tests/v2/test_autonomy.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
C:\Users\User\_projects\takeout-downloader-reliability\tests\v2\test_autonomy.py:23: in <module>
    ???
../../anaconda3/Lib/site-packages/fastapi/__init__.py:7: in <module>
    from .applications import FastAPI as FastAPI
../../anaconda3/Lib/site-packages/fastapi/applications.py:7: in <module>
    from fastapi import routing
../../anaconda3/Lib/site-packages/fastapi/routing.py:45: in <module>
    from fastapi import params
../../anaconda3/Lib/site-packages/fastapi/params.py:7: in <module>
    from fastapi.exceptions import FastAPIDeprecationWarning
../../anaconda3/Lib/site-packages/fastapi/exceptions.py:5: in <module>
    from pydantic import BaseModel, create_model
../../anaconda3/Lib/site-packages/pydantic/__init__.py:5: in <module>
    from ._migration import getattr_migration
../../anaconda3/Lib/site-packages/pydantic/_migration.py:4: in <module>
    from pydantic.warnings import PydanticDeprecatedSince20
../../anaconda3/Lib/site-packages/pydantic/warnings.py:5: in <module>
    from .version import version_short
../../anaconda3/Lib/site-packages/pydantic/version.py:7: in <module>
    from pydantic_core import __version__ as __pydantic_core_version__
../../anaconda3/Lib/site-packages/pydantic_core/__init__.py:8: in <module>
    from ._pydantic_core import (
E   ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
__________________ ERROR collecting tests/v2/test_pipeline.py __________________
ImportError while importing test module '/mnt/c/Users/User/_projects/takeout-downloader-reliability/tests/v2/test_pipeline.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
C:\Users\User\_projects\takeout-downloader-reliability\tests\v2\test_pipeline.py:19: in <module>
    ???
../../anaconda3/Lib/site-packages/fastapi/__init__.py:7: in <module>
    from .applications import FastAPI as FastAPI
../../anaconda3/Lib/site-packages/fastapi/applications.py:7: in <module>
    from fastapi import routing
../../anaconda3/Lib/site-packages/fastapi/routing.py:45: in <module>
    from fastapi import params
../../anaconda3/Lib/site-packages/fastapi/params.py:7: in <module>
    from fastapi.exceptions import FastAPIDeprecationWarning
../../anaconda3/Lib/site-packages/fastapi/exceptions.py:5: in <module>
    from pydantic import BaseModel, create_model
../../anaconda3/Lib/site-packages/pydantic/__init__.py:5: in <module>
    from ._migration import getattr_migration
../../anaconda3/Lib/site-packages/pydantic/_migration.py:4: in <module>
    from pydantic.warnings import PydanticDeprecatedSince20
../../anaconda3/Lib/site-packages/pydantic/warnings.py:5: in <module>
    from .version import version_short
../../anaconda3/Lib/site-packages/pydantic/version.py:7: in <module>
    from pydantic_core import __version__ as __pydantic_core_version__
../../anaconda3/Lib/site-packages/pydantic_core/__init__.py:8: in <module>
    from ._pydantic_core import (
E   ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
!!!!!!!!!!!!!!!!!!! Interrupted: 3 errors during collection !!!!!!!!!!!!!!!!!!!!
3 errors in 14.27s

EXIT 2
```

## Full C checkout suite
Exact argv: `["C:\\Users\\User\\anaconda3\\python.exe", "-B", "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider"]`
Environment: PYTHONDONTWRITEBYTECODE=1, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1. Cache disabled.
```text
........................................................................ [  5%]
........................................................................ [ 10%]
........................................................................ [ 15%]
........................................................................ [ 20%]
........................................................................ [ 25%]
........................................................................ [ 30%]
........................................................................ [ 35%]
........................................................................ [ 40%]
........................................................................ [ 45%]
........................................................................ [ 50%]
.....................................................................sss [ 55%]
sssssssssssssssssssssssssssssssssssssssssss..sssss...................... [ 60%]
........................................................ssssss.......... [ 66%]
........................................................................ [ 71%]
........................................................................ [ 76%]
........................................................................ [ 81%]
........................................................................ [ 86%]
........................................................................ [ 91%]
..........sssssssss..................................................... [ 96%]
..................................................                       [100%]
============================== warnings summary ===============================
..\..\anaconda3\Lib\site-packages\fastapi\testclient.py:1
  C:\Users\User\anaconda3\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

manager\app.py:91
  C:\Users\User\_projects\takeout-downloader-reliability\manager\app.py:91: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.

          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).

    @app.on_event("startup")

..\..\anaconda3\Lib\site-packages\fastapi\applications.py:4681
..\..\anaconda3\Lib\site-packages\fastapi\applications.py:4681
  C:\Users\User\anaconda3\Lib\site-packages\fastapi\applications.py:4681: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.

          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).

    return self.router.on_event(event_type)  # ty: ignore[deprecated]

manager\app.py:104
  C:\Users\User\_projects\takeout-downloader-reliability\manager\app.py:104: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.

          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).

    @app.on_event("shutdown")

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
SKIPPED [1] tests\v3\test_container_watch.py:199: Unix file modes/symlinks unconfirmed on Windows
SKIPPED [5] tests\v3\test_container_watch.py:260: Linux subreaper/pidfd/EOF behavior unconfirmed on Windows
SKIPPED [1] tests\v3\test_container_watch.py:301: Linux subreaper/pidfd/EOF behavior unconfirmed on Windows
SKIPPED [38] tests\v3\test_container_watch.py:315: Linux subreaper/pidfd/EOF behavior unconfirmed on Windows
SKIPPED [1] tests\v3\test_container_watch.py:355: Linux subreaper/pidfd/EOF behavior unconfirmed on Windows
SKIPPED [3] tests\v3\test_container_watch.py:410: Linux subreaper/pidfd/EOF behavior unconfirmed on Windows
SKIPPED [1] tests\v3\test_container_watch.py:465: Linux subreaper/pidfd/EOF behavior unconfirmed on Windows
SKIPPED [1] tests\v3\test_container_watch.py:485: Linux subreaper/pidfd/EOF behavior unconfirmed on Windows
SKIPPED [1] tests\v3\test_http_client_live.py:131: live network test � set AUTOPILOT_LIVE=1 to run
SKIPPED [1] tests\v3\test_http_client_live.py:147: live network test � set AUTOPILOT_LIVE=1 to run
SKIPPED [1] tests\v3\test_http_client_live.py:159: live network test � set AUTOPILOT_LIVE=1 to run
SKIPPED [1] tests\v3\test_http_client_live.py:180: live network test � set AUTOPILOT_LIVE=1 to run
SKIPPED [1] tests\v3\test_http_client_live.py:196: live network test � set AUTOPILOT_LIVE=1 to run
SKIPPED [1] tests\v3\test_http_client_live.py:225: live network test � set AUTOPILOT_LIVE=1 to run
SKIPPED [8] tests\v3\test_secret_mount_boundary.py:107: real symlinks require Linux fixture
SKIPPED [1] tests\v3\test_secret_mount_boundary.py:124: real symlinks require Linux fixture
1352 passed, 66 skipped, 5 warnings in 117.53s (0:01:57)

EXIT 0
```

## WSL Linux executable suites (dependency-scoped)
The unrestricted v3/v2 collection above is BLOCKED by unavailable Linux pydantic_core (Windows extension cannot load on Linux). No installs or global configuration changes. Exclude only the three FastAPI-dependent v2 modules for this run; do not represent it as full Linux coverage.
Exact argv:
```json
["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", "cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/mnt/c/Users/User/anaconda3/Lib/site-packages python3 -B -m pytest -q -rs -p no:cacheprovider tests/v3 tests/v2 --ignore=tests/v2/test_api.py --ignore=tests/v2/test_autonomy.py --ignore=tests/v2/test_pipeline.py"]
```
```text
........................................................................ [  6%]
........................................................................ [ 12%]
........................................................................ [ 18%]
...............................ssssss................................... [ 24%]
..........................................F............................. [ 30%]
........................................................................ [ 36%]
........................................................................ [ 42%]
.....................F.................................................. [ 48%]
........................................................................ [ 55%]
........................................................................ [ 61%]
........................................................................ [ 67%]
........................................................................ [ 73%]
........................................................................ [ 79%]
........................................................................ [ 85%]
........................................................................ [ 91%]
........................................................................ [ 97%]
..........................                                               [100%]
=================================== FAILURES ===================================
______________ test_the_minted_urls_are_cached_for_the_next_pass _______________

tmp_path = PosixPath('/tmp/pytest-of-user/pytest-0/test_the_minted_urls_are_cache0')

>   ???
E   AssertionError: the second pass should transfer
E   assert []
E    +  where [] = <fake_http.FakeHttpClient object at 0x7bd450316d50>.requests

C:\Users\User\_projects\takeout-downloader-reliability\tests\v3\test_mint_only.py:76: AssertionError
_______________ test_owned_spawned_descendant_terminated[flood] ________________

tmp_path = PosixPath('/tmp/pytest-of-user/pytest-0/test_owned_spawned_descendant_1')
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7bd450bda660>
mode = 'flood'

>   ???
E   AssertionError: regression must actually spawn and run the owned child
E   assert False
E    +  where False = <bound method Path.exists of PosixPath('/tmp/pytest-of-user/pytest-0/test_owned_spawned_descendant_1/ready')>()
E    +    where <bound method Path.exists of PosixPath('/tmp/pytest-of-user/pytest-0/test_owned_spawned_descendant_1/ready')> = PosixPath('/tmp/pytest-of-user/pytest-0/test_owned_spawned_descendant_1/ready').exists

C:\Users\User\_projects\takeout-downloader-reliability\tests\v3\test_remote.py:167: AssertionError
=========================== short test summary info ============================
SKIPPED [1] C:\Users\User\_projects\takeout-downloader-reliability\tests\v3\test_http_client_live.py:131: live network test — set AUTOPILOT_LIVE=1 to run
SKIPPED [1] C:\Users\User\_projects\takeout-downloader-reliability\tests\v3\test_http_client_live.py:147: live network test — set AUTOPILOT_LIVE=1 to run
SKIPPED [1] C:\Users\User\_projects\takeout-downloader-reliability\tests\v3\test_http_client_live.py:159: live network test — set AUTOPILOT_LIVE=1 to run
SKIPPED [1] C:\Users\User\_projects\takeout-downloader-reliability\tests\v3\test_http_client_live.py:180: live network test — set AUTOPILOT_LIVE=1 to run
SKIPPED [1] C:\Users\User\_projects\takeout-downloader-reliability\tests\v3\test_http_client_live.py:196: live network test — set AUTOPILOT_LIVE=1 to run
SKIPPED [1] C:\Users\User\_projects\takeout-downloader-reliability\tests\v3\test_http_client_live.py:225: live network test — set AUTOPILOT_LIVE=1 to run
2 failed, 1170 passed, 6 skipped in 332.85s (0:05:32)

EXIT 1
```

## WSL focused final mount/guardian/discovery gate and independent failure retest
Exact argv:
```json
["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", "cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/mnt/c/Users/User/anaconda3/Lib/site-packages python3 -B -m pytest -q -rs -p no:cacheprovider tests/v3/test_secret_mount_boundary.py tests/v3/test_container_watch.py tests/v3/test_discovery.py tests/v3/test_mint_only.py tests/v3/test_remote.py"]
```
```text
........................................................................ [ 24%]
........................................................................ [ 49%]
........................................................................ [ 74%]
......................................F................................. [ 99%]
..                                                                       [100%]
=================================== FAILURES ===================================
______________ test_the_minted_urls_are_cached_for_the_next_pass _______________

tmp_path = PosixPath('/tmp/pytest-of-user/pytest-0/test_the_minted_urls_are_cache0')

>   ???
E   AssertionError: the second pass should transfer
E   assert []
E    +  where [] = <fake_http.FakeHttpClient object at 0x7a00879a60c0>.requests

C:\Users\User\_projects\takeout-downloader-reliability\tests\v3\test_mint_only.py:76: AssertionError
1 failed, 289 passed in 129.98s (0:02:09)

EXIT 1
```

## WSL final focused admission/owned-process/discovery suites
Exact argv:
```json
["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", "id -u; python3 --version; cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=/mnt/c/Users/User/anaconda3/Lib/site-packages python3 -B -m pytest -q -rs -p no:cacheprovider tests/v3/test_secret_mount_boundary.py tests/v3/test_container_watch.py tests/v3/test_discovery.py"]
```
```text
1000
Python 3.12.3
........................................................................ [ 28%]
........................................................................ [ 57%]
........................................................................ [ 86%]
...................................                                      [100%]
251 passed in 60.86s (0:01:00)

EXIT 0
```

## Static checks and origins
In-memory compilation PASS for 151 Python sources (no bytecode writes). Both Compose YAML and example JSON parse PASS. Offline unit UID/GID, mandatory EnvFile and bounded recovery assertions PASS.
```json
{
  "autopilot": "C:\\Users\\User\\_projects\\takeout-downloader-reliability\\autopilot\\__init__.py",
  "autopilot.discovery": "C:\\Users\\User\\_projects\\takeout-downloader-reliability\\autopilot\\discovery.py",
  "autopilot.watch": "C:\\Users\\User\\_projects\\takeout-downloader-reliability\\autopilot\\watch.py",
  "tools.container_watch": "C:\\Users\\User\\_projects\\takeout-downloader-reliability\\tools\\container_watch.py"
}
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n", "/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/chromium-service.sh"]`
```text
/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/chromium-service.sh: line 18: syntax error near unexpected token `$'do\r''
/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/chromium-service.sh: line 18: `for _ in $(seq 1 60); do
'

EXIT 2
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n"]`
Input: corresponding repository shell bytes with CRLF normalized IN MEMORY ONLY.
```text

EXIT 0
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n", "/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/cloudflared/setup-tunnel.sh"]`
```text
/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/cloudflared/setup-tunnel.sh: line 63: syntax error: unexpected end of file

EXIT 2
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n"]`
Input: corresponding repository shell bytes with CRLF normalized IN MEMORY ONLY.
```text

EXIT 0
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n", "/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/cloudflared/verify-exposure.sh"]`
```text
/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/cloudflared/verify-exposure.sh: line 77: syntax error: unexpected end of file

EXIT 2
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n"]`
Input: corresponding repository shell bytes with CRLF normalized IN MEMORY ONLY.
```text

EXIT 0
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n", "/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/init_custom.sh"]`
```text

EXIT 0
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n"]`
Input: corresponding repository shell bytes with CRLF normalized IN MEMORY ONLY.
```text

EXIT 0
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n", "/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/manager-service.sh"]`
```text
/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/manager-service.sh: line 15: syntax error near unexpected token `$'do\r''
/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/manager-service.sh: line 15: `for _ in $(seq 1 30); do
'

EXIT 2
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n"]`
Input: corresponding repository shell bytes with CRLF normalized IN MEMORY ONLY.
```text

EXIT 0
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n", "/mnt/c/Users/User/_projects/takeout-downloader-reliability/webgui/reload-extension.sh"]`
```text

EXIT 0
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-n"]`
Input: corresponding repository shell bytes with CRLF normalized IN MEMORY ONLY.
```text

EXIT 0
```

Exact argv: `["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", "cd /mnt/c/Users/User/_projects/takeout-downloader-reliability && systemd-analyze verify --man=no deploy/takeout-autopilot-watch.service"]`
```text
Configuration file /mnt/c/Users/User/_projects/takeout-downloader-reliability/deploy/takeout-autopilot-watch.service is marked executable. Please remove executable permission bits. Proceeding anyway.
Configuration file /mnt/c/Users/User/_projects/takeout-downloader-reliability/deploy/takeout-autopilot-watch.service is marked world-writable. Please remove world writability permission bits. Proceeding anyway.

EXIT 0
```

Exact argv: `["C:\\Users\\User\\anaconda3\\python.exe", "-B", "tools/container_watch.py", "--help"]`
```text
usage: container_watch.py [-h] [--inside] [--container CONTAINER]

                          [--identity IDENTITY] [--config CONFIG]



Foreground Docker bridge and Linux-only, scoped worker guardian. Host runs

with Docker authority; only the dedicated numeric UID/GID runs inside. No

secret values are placed in exec argv. EOF or a missed stdin heartbeat closes

all descendants of the in-container subreaper, including new sessions. This is

not a container restart/stop facility and never signals unrelated processes.



options:

  -h, --help            show this help message and exit

  --inside

  --container CONTAINER

  --identity IDENTITY

  --config CONFIG


EXIT 0
```

All 6 shell scripts checked. Normalized syntax failures: []. Raw DrvFS failures (unchanged CRLF files): ["webgui/chromium-service.sh", "webgui/cloudflared/setup-tunnel.sh", "webgui/cloudflared/verify-exposure.sh", "webgui/manager-service.sh"]. No shell files edited. Unit verify exits 0; DrvFS executable/world-writable checkout-mode warnings retained. No installation, reload or activation. Earlier static wrapper attempts stopped on raw CRLF errors and Windows text-mode stdin CRLF translation; the binary-stdin checks above avoid both without changing files.

## Disposition and limitations

**Assigned mount-boundary repair implemented; not a blanket release/activation approval.**

- All three future archive binds are literal `/opt/archives:/opt/archives`. No inherited STORAGE_ROOT override can broaden them. webGUI manager STORAGE_ROOT is fixed `/opt/archives`; both CLI allowed roots are `/opt/archives:/downloads`. `/config`, `/work`, downloads/drop, read-only VFS cache and specific read-only legacy jfs002 mounts remain where originally supplied. Ports/auth settings are unchanged.
- Before supervisor Docker exec, a host-only `/usr/bin/docker inspect --type container --format '{{json .Mounts}}'` admission probe validates every returned mount. It has a 10s deadline, 1 MiB stdout bound, suppressed stderr, bounded owned-client kill/wait, and fixed refusal diagnostics. It resolves lexical and real source/protected paths and rejects overlap with the secrets directory for bind AND volume sources, regardless of readonly status. Empty/missing/malformed metadata, unsupported mount types, resolution errors, failed inspect, timeout and flood fail closed. No captured metadata or secret values are printed.
- Docker exec still uses numeric nonroot UID:GID and environment NAMES only. Existing Linux pidfd/subreaper/heartbeat ownership boundary is preserved. Tests never execute Docker: injected doubles or synthetic Python clients only. Real Linux symlink ancestor, exact-directory, readonly, volume, malformed/missing, timeout/flood and reported-safe-layout fixtures pass.
- This is point-in-time admission, NOT prevention of exposure when an administrator independently creates/reconfigures an unsafe container, changes mounts/symlinks, or otherwise bypasses the supervisor. **All effective container mounts must exclude host secrets BEFORE provisioning secrets.** The shipped VFS host-source override remains configurable; arbitrary administrator edits/overrides must be reviewed. The actual-layout fixture is synthetic evidence of compatibility, not a new live inspection.
- Future Compose correction only: no existing containers recreated, no data removed, no Docker/server/auth/credential/production-worker/service-activation/Git actions. No documentation or other source files edited. Core21495 deployment is not changed or recertified. Downloads remain disabled (commander-stated status; no live status action).

### Verification disposition

- Full exact C checkout: **1352 passed, 66 skipped, 5 warnings** (exit 0). The capture wrapper later failed printing a Unicode replacement character to the Windows cp1252 console; the full successful pytest result and exit 0 were already written above. This is not a test failure.
- Windows focused final code: **153 passed, 60 skipped**. Initial development collection typo and oversized pytest IDs were corrected before full gating; real symlink/Linux skips are covered by WSL.
- Nonroot WSL final mount/guardian/discovery: **251 passed, no skips** (exit 0).
- Unrestricted WSL v3/v2: collection **BLOCKED** by unavailable Linux pydantic_core in three existing FastAPI-dependent v2 modules. No dependency installation performed.
- WSL excluding only those three modules: **1170 passed, 6 skipped, 2 failed**. Unmodified `test_mint_only.py::test_the_minted_urls_are_cached_for_the_next_pass` fails its second-pass-transfer assertion and remains reproducible. Unmodified `test_remote.py::test_owned_spawned_descendant_terminated[flood]` failed its ready-marker timing assertion, then passed the focused repeat (combined repeat: **289 passed, 1 failed**, mint-only remains). No out-of-scope repair or hidden full-Linux-green claim.
- In-memory compile **151 Python sources PASS**; both Compose YAML files/example JSON parse PASS; module origins exact C checkout PASS; repository unit verification **exit 0** with unchanged DrvFS mode warnings. Six shell scripts pass `bash -n` with in-memory LF normalization; four unchanged CRLF scripts fail raw WSL syntax checks, as recorded above. No unit/script installation or edits.

### Fingerprints / scope evidence

The 173 pre-existing source/config/test hashes matched the prior frozen report at START. END differs in exactly the four assigned pre-existing files; 169 others are unchanged. One assigned test is new (174 comprehensive files total). Prior documentation-only banner mismatch is commander-acknowledged; this report does not rebaseline or amend that report.

Owned final SHA256:
```json
{
  "docker-compose.webgui.yml": "b201566a00aadd5132d2c3536968f9f0749811d44285a9ad0dc221724def9a5c",
  "docker-compose.yml": "33f8c6711b705539194920e22b9e11544ac91ce734a34af5a65d6157299422e8",
  "tools/container_watch.py": "0c9bee1d681f483715f0d23f225ea6eb0fbf9b6d99ef719bc2e74d3eeca26c72",
  "tests/v3/test_container_watch.py": "4fa8e926f4461caaa104afa7e2a413968477a58c7db28530de901b1233112323",
  "tests/v3/test_secret_mount_boundary.py": "7047312e7a27d110c873b8c7673dc8eb4abd9f23d2f1b9ceadfab0338b049f55"
}
```

END comprehensive aggregate (SHA256 of sorted compact JSON path/hash map): `7861289080b77e7eaf87c83a1554537ee0ddd9124dc941349069ee90b18eeab5`.

## END source/config/test fingerprints
```json
{
  "Dockerfile": "5e435a2cb23d37ac289c2897d90c75c3aeba79b8657fad1c08cdb831cabf9fda",
  "Dockerfile.takeout_dl": "b768345e0a87772b4a8b3ec8c18b260b1f876eb7764bc125894d1836b2996fd5",
  "aria2c_integration.py": "9faa16a9d0afa1d6c914665fe891c0695aa976809862c528d045d81115720ac9",
  "autopilot/__init__.py": "ffaa24a9d0e988b9bf5fd246354ae38fa67e93c4a276c0346bb37fe8717a59ce",
  "autopilot/__main__.py": "deeb474593b0bb6d2b871699e931018169cc72f0a6944ee035a68b703d72cead",
  "autopilot/cdp.py": "022b06529c8f949abce8d705fc7009b54e0639d45cc5413bd6f95a6e30325173",
  "autopilot/discovery.py": "93d601df0280893937268eb66a52234954d3b70a332e2306fc88011b3219130b",
  "autopilot/errors.py": "03044e0dde01de5af5dbf9e497274bd39a83a66d18cca09f0496c5a5778fc097",
  "autopilot/jar.py": "9e675020ad63fa14bff6663ccedcc5c5dec154896468f44762d6441604726484",
  "autopilot/ledger.py": "f72dfcb23b9d81e70bd1c6b3146b9900dc52ce705a2fa85f7ac3e6eb38b76629",
  "autopilot/mint.py": "eaff75151ec6c58e265984e72ddbd204dac70ec19fc48e894a80ad1464c16fb0",
  "autopilot/mover.py": "b4659a477441e076060148ddefa9cdd1b489c11700cea4fa274d1057b976cb56",
  "autopilot/remote.py": "7c474340b436b29f209131d0608ff6600b85f90d5fc48e2eb293a1805c84d72c",
  "autopilot/report.py": "9d7752da3eb9ef239f1e9b83318ea487bf64703714242d93f20644ca448ffa60",
  "autopilot/run.py": "7208d37f5b0c8bcc83a70a55a52653dbc6d998b12403ccca71601dd50779a3ed",
  "autopilot/scrape.py": "f28abd58d820a84626b1b083622731311e2d1561879414e35c80f5b3cd056292",
  "autopilot/transport.py": "9188b520c34a070c16505010264f46e7af771f9614713217e54c480df5bbf47d",
  "autopilot/verify.py": "594286ba82a23968d3b7021a1ce16f7f1816a7b9ce8d025a9ac49191bc870f0e",
  "autopilot/watch.py": "4f6381d43797eb3f839ea288b3937024b73cb4441f21720fb515ae59bf7404c2",
  "autopilot/ws_transport.py": "283709d867d9c6190c390e53b634cc75d039300a397faaf945f5d680e99cccc5",
  "build.py": "106c511ebc6299b16069637f4cd39422a35d67e73f51a1249168f932a7eba930",
  "cdp_reload_ext.py": "a614c70df47c02d161516cf77dc78be4e58d826585a4a833597fe443625e0c41",
  "conftest.py": "855ab46fb3034b4a3baa5ceb992358dc1bea52502665f71ef998953474ef775a",
  "dedupe_takeout.py": "420d31c166dda9dfbb1e1fb99bd0c5e86b41edd8f21253e12ddc03df9f0a60e8",
  "deploy/takeout-autopilot-watch.service": "3a073ea144716e6cf74dfbd818649b866d2767d7d44bc352426c6a595739d3dc",
  "deploy/watch.example.json": "3d6b5a73e5cc25ccb10643cb2e1b4217c5f7587a83392d31071bd72901a258a7",
  "docker-compose.webgui.yml": "b201566a00aadd5132d2c3536968f9f0749811d44285a9ad0dc221724def9a5c",
  "docker-compose.yml": "33f8c6711b705539194920e22b9e11544ac91ce734a34af5a65d6157299422e8",
  "google_takeout_tui.py": "0f05b580c12f304e042ace2ff2b32dbf6dcbaea5a7787064d3bf49b1d786bdee",
  "helpers/manifest.json": "a7fe5b3ff15ad069abc0bd0b17650b7b52b54231167adcd57562281a93795cc1",
  "manager/__init__.py": "f76587ca210b223751f87c250ef619ea84d4391c5fecc679c27c0035c4221cc5",
  "manager/__main__.py": "1ee571c0408fa568b92ca4ddaaa3be87078ac684994ec8f0be48eedd2185850f",
  "manager/app.py": "e7d0c3b32c615cbb00235f850c148ac9df236c190a36e9874370610fe298e95f",
  "manager/config.py": "63d5148f2e948d325946d2e68112cc9381cecbe52113403bd5fde301deafa36a",
  "manager/derive.py": "5aa5da8c9c87d06f56ec2ae1bf0dd6f1567f1520a12408c5babdc63caa8901c6",
  "manager/diagnose.py": "0ac2ad445befe22c6b15846f2c26d31b183a084ba9411a4c0f2d31aa311398dd",
  "manager/engine_bridge.py": "91523c1d7484e2c1752bc47ebf288d1bbb36f98320f19d1d4db88a4f372c37a4",
  "manager/jobs.py": "3e56cc3be6dd3f5e05b4b0c1f425bfabd1898e79098c69366b7d584a53049540",
  "manager/manifest.py": "b1859e6c56e83151c80669dad5beaa72c5688777dfbe0d957197aa23bf268691",
  "manager/notify.py": "265d41dd94d74f294534088c8c1c1ff0843446b59cf4b215dded8862d8d8e04e",
  "manager/orchestrator.py": "ff2d7ae2854a9c087da69bfaca1a1abcdf375e8d8de4213edd401495fa24c1a7",
  "manager/recipes.py": "5bf833f21e9e95381aac312af617bf2cd7d569f025f95af14dad76b15ab4ab01",
  "manager/requirements.txt": "e87473ee80bd56be8ca39b0eb55d46a0ae6a36c06e8a079269409020b49f695c",
  "manager/tests/test_e2e_phase2.py": "a9e105f0a308c0465cd6a25352fa3f0c6274ae805ac63accce3dd51b90533983",
  "manager/tests/test_phase4_control.py": "e6b48ced9bafd894885860ca8661e9a0c04f5ffe9663c3c004d06b849d224dc8",
  "manager/tests/test_phase5_v4contract.py": "f7a731a2b5fbe432ec4ef53dffc8775cfc4ea9a5c029f5d172edf4af8ad85fa0",
  "manager/tests/test_phase8_telegram.py": "265eb39812d0822858d22ff7f8a7e55e9c45a5919ecb7aaee4c65983761e7d1c",
  "manager/tests/test_phase9_recipes.py": "67a3e2a0fc33d4ce9155ba4fc520df9fee5c446aa4a49456d121fb26b7dda5b3",
  "manager/tests/test_review_fixes.py": "0e18421221b68c7646da21b861beab63b26b7c982f57bd8df2fa2b4002ebdcf3",
  "manager/v2integration.py": "99afea101addd8c974f59d53937cd4ec1e9c6464320b18d70db4b4e5ea7f2d4b",
  "monitor_job.py": "7d8ed73598acb08f2d3ce15b68a0beeffd12ec699f037a70289345ba9e73ee65",
  "paste_server.py": "82cddc318914d7cce709193fe8329bfd3f85d3afc1966b287a41ab616b8397f0",
  "pytest.ini": "ca4b82760538130959a0fbe25c21fc5d28cd46fb7a63b1209def6d19e2b40a62",
  "requirements-autopilot.txt": "24be066fd8d53d76c9d14033dae93d05755d75a11808d179f454b4ef36495df1",
  "requirements-dev.txt": "c7c71a42aa7fcda7d4b026f6aeb29e93265af099ed49d922023ea139cc43e0da",
  "requirements.txt": "cd0ac27fb61eaab8cac6427248ed57d6c02d5299ee5ae6a4d15691c4c8d19a06",
  "security/__init__.py": "bb5907ad0c992dcc380137fcf7888ed66762bb9000ca5ab13ede4368077de5b9",
  "security/c2_resolver.py": "1895f14aa2286f5d81fdc6608635e0368d527679443d4c5488661a510044bbd4",
  "security/detector.py": "7bc92a83148fc6f2651e810fc9f4fb9f45cfd1b933f26239179b28931dd759d0",
  "security/fixture_generator.py": "5bd9822f9d76a3b02d93feb93c9fea55293fb8ade11e828f0156e14a57ec16e1",
  "security/obfuscation_lab.py": "b57e81df276e552161905a0d11849f85ed8fe9f1406eaea7cd4e6f719a53caf2",
  "security/sandbox.py": "53a5197d6c426700254498ccc514541a505e83232996d24ffe6b5593d597044f",
  "takeout.py": "fca317e73eb591d0ba5dee7e86dbd071719396629bad9a823d1c3f7222006b05",
  "takeout2/__init__.py": "dcbfc057afbcd232bfc71a49c082ff9e1c461ebb62583997501d86652332eae0",
  "takeout2/__main__.py": "7328bda130ffaabe956dbcc8b0be95b7f514350536b638f7e605bf285d1ef188",
  "takeout2/api.py": "7d476f4628c8af5a1ead80b82dc12ca1b44d59b4a786712e2a20e7a44eeecdd4",
  "takeout2/backoff.py": "98b5fbaa847271177183321dd0197d3ed8a9227149e10e29645e5116ace83f22",
  "takeout2/cachewatch.py": "5052a21a430f220877e6e1ea114762cba23319bcae96708e55a56593c0fab6aa",
  "takeout2/classify.py": "637d0bdb767246738534dae0517070c1b4da6c8ebfa5590c5a9a802f40b1940f",
  "takeout2/cli.py": "64055fa8c1d5948e03fce3f88ae16729bc4ce193e5713a7933c09a04987d1b30",
  "takeout2/contracts.py": "b6772872ef5397abc53e687a8e5c8c6b8b4d3c27c25456b186f9efc0234bdf17",
  "takeout2/cookie.py": "50032ff97e775047533f4c4fa23577c0d3f26b1678e2e25b5c9f724fdea63605",
  "takeout2/engine.py": "d10d1d68276e5b2d965a18c51469e24499baf38740c7e292deb305ec2a5bfb59",
  "takeout2/experiment.py": "5ec5f07bbc7e1ec2cfd5bda0c65cce3f67e174f795399256eb1457a176f7f318",
  "takeout2/ledger.py": "39bae33c451f2dce766f3029864c4ed74df6abff0e54fdbfa29a6eb67418f68e",
  "takeout2/migrate.py": "79ef4b76e6e859e8ec6b61584ba6ce26b945da197e6ef2cc391973682c537cf8",
  "takeout2/plan.py": "22a9c32aad8d40374aec25df9758485b447ceae5bebf330af1ef24b9f60ceb47",
  "takeout2/preflight.py": "21b78d068b35afdb44c2f77826f03ba288157813e7166d1e59c2f13cb5e9a300",
  "takeout2/progress.py": "ccf3d16f961d84c11e557159bbce109a0337bae008ec994024ed4acc72fc2b10",
  "takeout2/runner.py": "b8d818b3c32c5b67083e0fcfe17c6e7668374fd01f129271bd4d2a91aba1c6be",
  "takeout2/state.py": "be9e6d34de925d22a5cc2a02f5ed930e99dda0224d9781fee0c2a2248ab61d47",
  "takeout2/verify.py": "c92a52bbbc1e1eb03145ec1375d271f63e8b8261410e589806e6e654b589d82e",
  "takeout_cli.py": "a06f33860791f3dff055221b58cf1919b546bb2c11175e69d3ded9da1522662b",
  "takeout_cli_analyze.py": "026008fe8c65c11c69180af8619b3a5efc6dd7aab5b205501d8433399a1bafb1",
  "takeout_dl.py": "4376a9ab755652445e324161650680dcba4e70d04412eeb7996b56f115a55822",
  "takeout_downloader.py": "4542c5bfa655fbc44ffac76df9515b6a57e6ee5803bd176bed7ec35e5a16c9f1",
  "takeout_payload.py": "94fee88e43b615ac427d603abf42b6b278cfbb5ace151b370af945f02175fd55",
  "tests/fixtures/sample_multi_payload.json": "dea786c8dda6a9ad8dec552f193a005fa6d8f4b37341c9c5523b9ebc5fef6c49",
  "tests/fixtures/sample_payload.json": "33c1881b18fc31a104fc9381985062447ce7506f63185fc23e24ff08be07b6a9",
  "tests/test_internal_downloader.py": "21729f4413d1384a064e5b18822676105ee1b8bd8bc60ced2a4d1fab4103bbfa",
  "tests/test_paste_server.py": "d5549d8510b77ee72759eebe16e07fc18f915f4f843b044270e5b73214d70fcd",
  "tests/test_security.py": "f10f335cb00fe6d918937071a8924aaf93def580550610f4bc6985aac1fd6a66",
  "tests/test_takeout_cli.py": "6856ef29214fd0e464cd24cf15209c492403b2063d70d59074b7ef23186b4ca1",
  "tests/test_takeout_payload.py": "25b9931f17f50565cc908c10dbf407383acf060a3da285609e9786137270fca5",
  "tests/v2/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "tests/v2/test_api.py": "708dd4d146d8a68b276fd87eac1d7af3dc1d3dee9db4332b2f984a918a28a6ce",
  "tests/v2/test_autonomy.py": "d3e8693a7c842fa428f6c660fb7501d003f6f2243edf5fbf6b21306593f1912b",
  "tests/v2/test_backoff.py": "9c38065913cfe87a27518c06883de0487aa860d7fdb7a499b12750b25ddff1b2",
  "tests/v2/test_cachewatch.py": "d16a4b86d70a143b8b38f08b6065781ae0eaae5848c087637a6f585e8ff453cf",
  "tests/v2/test_classify.py": "6b125bb2337a72f1fe2880d367e6d507b20677c8817372cccefb750290baf637",
  "tests/v2/test_cli.py": "41df9876e132a0b6d91406fa9c5923429b937982c4eabc5178ec92e6f642f3ce",
  "tests/v2/test_cli_ux.py": "a3bf29ef952d80f1e577a7baa678c993c3bb720126f10dc378d964b2e52b088f",
  "tests/v2/test_contracts.py": "892c491a33ef0858eb2586eb88c589a0832fb4664357741b5f647d3e74f91867",
  "tests/v2/test_cookie.py": "11913fb03ccc8414da9038b29f08449145ea2d4fc1e0252312f79a4384c416b0",
  "tests/v2/test_engine.py": "598357e546b7e8468925a7acadd97c357b6d3307e3e731f431cb697a7274caf8",
  "tests/v2/test_engine_hardening.py": "2b329b96684f923ce997dce23891d62eb0bd317f7f8d8b4b9af98220e2bd44c3",
  "tests/v2/test_experiment.py": "4a53e942cfcf969639ad09df649f573a872889ab3e1552379fa70adb7980cfcb",
  "tests/v2/test_ledger.py": "4dfb645eb4809210167ace8a151a69e12b172c8155c4618770e039875779c9a1",
  "tests/v2/test_migrate.py": "e6c7c570347b871d66690acece90aa54b99ae481a83e620ae7fdd31083ad7c06",
  "tests/v2/test_pipeline.py": "6bec4dd0a9f038d472fb6608c80215a0d97607bb3f55cd9fc979e5b00880a335",
  "tests/v2/test_plan.py": "6355eed3681b7e55062bad26a4079256c322e320d504e4a9767d41a0e29de4c5",
  "tests/v2/test_preflight.py": "b70383f7c72b6a188bd9769f862e84b1fa8edd707cec1647abbad233496b025d",
  "tests/v2/test_progress.py": "ba4257dc2147df03d1696a4f271ccdf144bf894928ce700f944e418b8adf3f9e",
  "tests/v2/test_runner.py": "c314b55964e2ed3e50d903b73e7e9aed311bfa55b7420c1088364a4c77573239",
  "tests/v2/test_state.py": "a7277a88f9633bf8c7ccdf45c726aafebb75b27e50e3ab33a53b03301e1e3088",
  "tests/v2/test_verify.py": "f5c59bee5415d25732e55e6fef7c6b3ead3c20d745d4e6c0725d7e39682a3cbf",
  "tests/v3/fake_cdp.py": "2243ab6e7c6cb2d52924074e0cfd983e42191dceda925cd9e0c8c07ab5bad180",
  "tests/v3/fake_http.py": "2e4847909334cc7ae3c8c6c96e972df793b19d79326cc96d83dc93d2038c13f0",
  "tests/v3/orchestration_fixtures.py": "5f54443a81e6d483a6f03d320b013cb7f1bd60a55a3ae536348cd0105c8c4ea3",
  "tests/v3/test_cdp_longrun.py": "2e7d1367be54f6ae49e8056c38cc423d2ceb357b11da5d5dac5b795f0c5842f0",
  "tests/v3/test_cdp_target.py": "4709c1c41dc33c19717ae6cd87d0553763003c5db1dfdcdde96499e75165faec",
  "tests/v3/test_cli.py": "45da3a71107792b8e0c9ede0b4846f29c4964c72753c858bff144d0ef4571fca",
  "tests/v3/test_cli_remote_args.py": "1430448829a6aff9d35f977c1195e0636aa92a9e978985df9980a1d3ac96fae7",
  "tests/v3/test_container_watch.py": "4fa8e926f4461caaa104afa7e2a413968477a58c7db28530de901b1233112323",
  "tests/v3/test_destination_index_is_used.py": "3e8ef2f09ad78e1db6229ad9500892f17f6b3cf2caa934a64367de9ca30b5409",
  "tests/v3/test_discovery.py": "79a2a4f25ce29adc9d8f9ae8a14200d91e78b157174f037c2c47b4ea1db5b5c3",
  "tests/v3/test_exit_codes.py": "36d081418780e86184384ae3753677135bdb2b6f428ffe5be1ffa343d53ddee1",
  "tests/v3/test_extension_defaults.py": "6b716d66ff8c1872604d68df615f61fe59aa3b52e9062ccf0d52dae627eb4a07",
  "tests/v3/test_filename_encoding.py": "18314274f33a931bc5820311edee3ba0999b56f000b8eea80af1537c59118685",
  "tests/v3/test_http_client_live.py": "dcee53865ac79088c67a7c451d324435cf5c5bf4f05538173c9afc0b0f88be04",
  "tests/v3/test_imports_resolve.py": "a0139d2ea7da3ecf468fa9a503ded4fc915eb5b0524f675d9f3c5e4423d1c9ba",
  "tests/v3/test_integration.py": "adfc5dd6863d51cac5f7ccc8ad5666092b192b690b3394803cac84919b91529f",
  "tests/v3/test_jar.py": "977d83f59ba7cf607d61bc11cce8a3d040d8ae63ec8ab9cc4f98aa59fc44dbe2",
  "tests/v3/test_ledger.py": "e886037829f63ba001b14b299d174143f5edfbf364c40fab38f389798aec0a60",
  "tests/v3/test_mint.py": "a8e75c228f031e106d6ed487a0afd949632d7ed768949ef744f46fedb28d272e",
  "tests/v3/test_mint_only.py": "1c7a926c68baa74f44b9d0d0760af30081bce5f17d7429a5953a7efd7aa1ce59",
  "tests/v3/test_mint_stray_downloads.py": "da92b6dfbd22337cc5a1e02ba455f4704f511a458588c0654e32f0bbc6bba06f",
  "tests/v3/test_mover.py": "99bb68152380083f34eb10ea27416db08e6fdeae936d18f9664bf2eda378ae04",
  "tests/v3/test_mover_watchdog.py": "7db89d885a09e8c4ef51227f08fc175a419ead3a8673ca3276c1f5f2afc78ced",
  "tests/v3/test_orchestration_reliability.py": "ae021704948c732ed1c93b02e95703aceaf742fa58f87d6d5127ec667699a2e8",
  "tests/v3/test_part_filename_preserved.py": "5bb9fb6c291af887b211d60278ebe9e7fd55c81289e7f9b3459372b9bccbfd77",
  "tests/v3/test_provoke_rapt.py": "093a1a96e9d2dd8675c2e9a9562d14f7a17267d1e42839afca29334b5e5e1911",
  "tests/v3/test_quota.py": "76073a6bc495c3855268b8befec57e096be37e63107b00111020065cf97e1a6f",
  "tests/v3/test_rapt.py": "24c969c96cafb6e4bfed0bdd94819460c47c9679d124c6ea225696438857c01f",
  "tests/v3/test_refresh_and_filename.py": "94e1da0e166424cfe8a1a538a5364749bad84914351500c73d51adbd15615035",
  "tests/v3/test_remote.py": "62ec8337aad48719603829aa5b835552372fb6f8fd6765ce2f9cd91fad3a391e",
  "tests/v3/test_report.py": "a6b5b8e6b683c50be3c550be87c91872b8449aa871dd3fcd569edc9a485e9457",
  "tests/v3/test_report_attempts.py": "130112a327b39d00c6c904506c951a1888e81977bff8a451deb2a2e971b6d9af",
  "tests/v3/test_report_sees_the_archive.py": "e12e1b0fcb53d7d57b7498e6b766bf38def7553acb074732743410f269a84be3",
  "tests/v3/test_review_fixes.py": "ae440575030ec05921bd2ed7f29c3e5b10bd6c37d334d8b7b6261f9ac82d1e5e",
  "tests/v3/test_run_destination_guard.py": "b9328270b22f4522fdd465f3d8a6965ebed2643a33c308292421ed4fe025c282",
  "tests/v3/test_scrape.py": "a5e3d3ccc8e249ec562aa36fe12dcdd0cd6d74746f716544cc4d58f0a0e5240a",
  "tests/v3/test_signin_classification.py": "73f3178eeb19a67f96abc63a1ef4e5cd5b4b7c557bac68a20f26e0b8cc68deee",
  "tests/v3/test_transfer_integrity.py": "71ee8c6f2c25cf237f0dc818838ba8a018e02abbe0475bc3d3a3d12297abab44",
  "tests/v3/test_transfer_without_browser.py": "c4c3d7e93ac981c024b791ca4a05b29f0c7a2fc9a882ba6869e3a2d21bf7b28b",
  "tests/v3/test_transport.py": "be7f6ca1cc60a95ccabfd3cecce5c1e44f512e7eea2185bdb31798eb079b2dfc",
  "tests/v3/test_verify.py": "d882fce0efa5a3540b6a7e35d020ea950a1478f8faa2fdab78137d988f5cb3f7",
  "tests/v3/test_watch.py": "47f5ab9ca651a57ac39303cb05867e19191be229872ff278b2d5bc77b5da6a22",
  "tools/container_watch.py": "0c9bee1d681f483715f0d23f225ea6eb0fbf9b6d99ef719bc2e74d3eeca26c72",
  "tools/extension_switch.py": "e10c7bae805a95435d928d24ea944af9baad171be4caf2e7e57fa31104ed227a",
  "tools/repair_part_filenames.py": "8f651c7d7dc6571d94da750a772426ed43fb14a21583d92e5c210bb4f8a69a94",
  "tools/satisfy_login.py": "79668282962e9f211c227f61f801e5692160152a726e104d8180b24a05e8d944",
  "tools/sweep_downloads.py": "e3902cc80baa6e546a45416e65558ded304ca3c07e208602b2788c326f7c3e07",
  "tools/xwd_to_png.py": "b04d5ed48f4a919565f10cad8c704592dd78cabf5fea7190943caed519a7f737",
  "webgui/Dockerfile.webtop": "755c4f13a87f9f77dfc3a7111dba47dcfc9cd9721d62ad790bd11a780f70fda1",
  "webgui/chromium-service.sh": "e648aa1c6844b5b16d6d15a305556d2a78e981b6bfaff6e6650dbeeffdeaddfa",
  "webgui/cloudflared/config.yml": "3a4a5da59991711907c810f44981ab655dfe5ef3fb7468c2fc18316466cb15b1",
  "webgui/cloudflared/setup-tunnel.sh": "780ac490873df9eef70043bde73bb9a1549458c93c749ad6c05532a1addc1a76",
  "webgui/cloudflared/verify-exposure.sh": "8ce669fa1843d1752e3eca4a258c9e2cba8e128595338658e9b2077cabb25bce",
  "webgui/init_custom.sh": "f8615cfcfa6fc46660a110c55b02ca71ee01ecf1872cacb530320e1bc769f878",
  "webgui/manager-service.sh": "d482ba894a26c4690f0c9ab0d6c69d7fe9811c9df43a408d7c488195447cf8dd",
  "webgui/profile-seed/managed-policy.json": "3c06963bda1f0abbdc70821c1d8d27f73bd101d1581dd4ed25625283b77773a8",
  "webgui/reload-extension.sh": "a2da4273037559a89fc38f8ad59ee35d4f512444b67b04204a74f6add4246cf8",
  "tests/v3/test_secret_mount_boundary.py": "7047312e7a27d110c873b8c7673dc8eb4abd9f23d2f1b9ceadfab0338b049f55"
}
```
