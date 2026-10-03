document.addEventListener("DOMContentLoaded", function () {

    // =========================================================
    // AI FARM ADVISOR
    // =========================================================

    var parts = window.location.pathname.split("/").filter(Boolean);
    var farmerId = parts[parts.length - 1];

    var aiCrop = document.getElementById("aiCrop");
    var aiQuantity = document.getElementById("aiQuantity");
    var aiQuestion = document.getElementById("aiQuestion");
    var aiLanguage = document.getElementById("aiLanguage");

    var aiRecommendBtn = document.getElementById("aiRecommendBtn");
    var aiLoading = document.getElementById("aiLoading");

    var aiResult = document.getElementById("aiResult");
    var aiRecommendationText =
        document.getElementById("aiRecommendationText");

    var voiceBtn = document.getElementById("voiceBtn");
    var voiceStatus = document.getElementById("voiceStatus");

    var speakRecommendationBtn =
        document.getElementById("speakRecommendationBtn");


    // =========================================================
    // GET AI RECOMMENDATION
    // =========================================================

    if (aiRecommendBtn) {

        aiRecommendBtn.addEventListener("click", function () {

            var crop = aiCrop ? aiCrop.value.trim() : "";
            var quantity = aiQuantity ? aiQuantity.value.trim() : "";
            var question = aiQuestion ? aiQuestion.value.trim() : "";
            var language = aiLanguage ? aiLanguage.value : "en";


            // Check input
            if (!crop && !question) {
                alert("Please enter a crop or ask a question.");
                return;
            }


            // Loading
            aiRecommendBtn.disabled = true;

            if (aiLoading) {
                aiLoading.style.display = "inline";
            }

            if (aiResult) {
                aiResult.style.display = "none";
            }


            // Send request to Flask
            fetch(
                "/api/farmer/" +
                farmerId +
                "/ai-recommendation",
                {
                    method: "POST",

                    headers: {
                        "Content-Type": "application/json"
                    },

                    body: JSON.stringify({
                        crop: crop,
                        quantity: quantity,
                        question: question,
                        language: language
                    })
                }
            )

            .then(function (response) {

                return response.json().then(function (data) {

                    return {
                        ok: response.ok,
                        data: data
                    };

                });

            })

            .then(function (result) {

                aiRecommendBtn.disabled = false;

                if (aiLoading) {
                    aiLoading.style.display = "none";
                }


                // Error
                if (
                    !result.ok ||
                    !result.data.success
                ) {

                    alert(
                        result.data.error ||
                        "Unable to get recommendation."
                    );

                    return;
                }


                // Show recommendation
                if (aiRecommendationText) {

                    aiRecommendationText.textContent =
                        result.data.recommendation;

                }

                if (aiResult) {
                    aiResult.style.display = "block";
                }

            })

            .catch(function (error) {

                console.error(
                    "AI Error:",
                    error
                );

                aiRecommendBtn.disabled = false;

                if (aiLoading) {
                    aiLoading.style.display = "none";
                }

                alert(
                    "Connection error. Please check that Flask is running."
                );

            });

        });

    }



    // =========================================================
    // VOICE INPUT
    // =========================================================

    var SpeechRecognition =
        window.SpeechRecognition ||
        window.webkitSpeechRecognition;

    var recognition = null;


    if (SpeechRecognition && voiceBtn) {

        recognition = new SpeechRecognition();

        recognition.continuous = false;

        recognition.interimResults = false;

        recognition.maxAlternatives = 1;


        // Voice button
        voiceBtn.addEventListener(
            "click",
            function () {

                var language =
                    aiLanguage ?
                    aiLanguage.value :
                    "en";


                // Select voice language
                if (language === "ta") {

                    recognition.lang = "ta-IN";

                }
                else if (language === "hi") {

                    recognition.lang = "hi-IN";

                }
                else {

                    recognition.lang = "en-IN";

                }


                if (voiceStatus) {

                    voiceStatus.textContent =
                        "🎤 Listening... Please speak now.";

                }


                voiceBtn.disabled = true;


                try {

                    recognition.start();

                }
                catch (error) {

                    console.log(
                        "Voice start error:",
                        error
                    );

                    voiceBtn.disabled = false;

                }

            }
        );


        // Voice result
        recognition.onresult =
            function (event) {

                var transcript =
                    event.results[0][0].transcript;


                if (aiQuestion) {

                    aiQuestion.value =
                        transcript;

                }


                if (voiceStatus) {

                    voiceStatus.textContent =
                        "✅ Voice captured.";

                }


                voiceBtn.disabled = false;

            };


        // Voice error
        recognition.onerror =
            function (event) {

                console.log(
                    "Speech error:",
                    event.error
                );

                voiceBtn.disabled = false;


                if (voiceStatus) {

                    if (
                        event.error ===
                        "not-allowed"
                    ) {

                        voiceStatus.textContent =
                            "❌ Microphone permission denied.";

                    }
                    else {

                        voiceStatus.textContent =
                            "❌ Voice input failed. Please try again.";

                    }

                }

            };


        // Voice ended
        recognition.onend =
            function () {

                voiceBtn.disabled = false;

            };

    }

    else if (voiceBtn) {

        voiceBtn.disabled = true;

        if (voiceStatus) {

            voiceStatus.textContent =
                "Voice input is not supported in this browser.";

        }

    }



    // =========================================================
    // TEXT TO SPEECH
    // =========================================================

    if (speakRecommendationBtn) {

        speakRecommendationBtn.addEventListener(
            "click",
            function () {

                var text =
                    aiRecommendationText ?
                    aiRecommendationText.textContent :
                    "";


                if (!text) {
                    return;
                }


                // Browser support check
                if (!window.speechSynthesis) {

                    alert(
                        "Text-to-speech is not supported in this browser."
                    );

                    return;

                }


                // Stop previous speech
                window.speechSynthesis.cancel();


                var speech =
                    new SpeechSynthesisUtterance(text);


                var language =
                    aiLanguage ?
                    aiLanguage.value :
                    "en";


                // Select speech language
                if (language === "ta") {

                    speech.lang = "ta-IN";

                }
                else if (language === "hi") {

                    speech.lang = "hi-IN";

                }
                else {

                    speech.lang = "en-IN";

                }


                speech.rate = 0.9;

                speech.pitch = 1;


                // Speak
                window.speechSynthesis.speak(
                    speech
                );

            }
        );

    }

});